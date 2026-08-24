import urllib.parse as urlparse

from posthog.schema import (
    CachedWebAgentAnalyticsQueryResponse,
    HogQLQueryResponse,
    WebAgentAnalyticsQuery,
    WebAgentAnalyticsQueryResponse,
    WebAgentAnalyticsQueryType,
    WebAgentContentGrouping,
)

from posthog.hogql import ast
from posthog.hogql.errors import QueryError
from posthog.hogql.parser import parse_expr, parse_select
from posthog.hogql.query import execute_hogql_query

from posthog.models.filters.mixins.utils import cached_property

from products.web_analytics.backend.hogql_queries.agent_analytics_definitions import (
    AGENT_ANALYTICS_DEFINITION_VERSION,
    AGENT_CATEGORIES,
    AGENT_CATEGORIES_WITH_CRAWLERS,
    AGENT_EVENTS,
    AGENT_HTTP_EVENT,
    AGENT_NAVIGATION_EVENTS,
    DEFAULT_CONVERSION_WINDOW_HOURS,
    DEFAULT_INACTIVITY_WINDOW_MINUTES,
    DEFAULT_MINIMUM_REQUESTS,
    DEFAULT_NAVIGATION_WINDOW_MINUTES,
    DEFAULT_RESULT_LIMIT,
    MAX_CONVERSION_WINDOW_HOURS,
    MAX_INACTIVITY_WINDOW_MINUTES,
    MAX_JOURNEY_STEPS,
    MAX_MINIMUM_REQUESTS,
    MAX_NAVIGATION_WINDOW_MINUTES,
    MAX_RESULT_LIMIT,
    accept_class_expr,
    agent_session_id_expr,
    bounded_int,
    malformed_path_expr,
    markdown_path_expr,
    normalized_path_expr,
    page_identity_expr,
    referrer_expr,
    response_content_type_expr,
    response_status_code_expr,
    scanner_path_expr,
    served_markdown_expr,
    static_asset_expr,
)
from products.web_analytics.backend.hogql_queries.web_analytics_query_runner import WebAnalyticsQueryRunner

OVERVIEW_QUERY = r"""
SELECT
    uniqIf(distinct_id, included_request AND {current_period}) AS active_clients,
    uniqIf(bot_name, included_request AND bot_name != '' AND {current_period}) AS agent_families,
    countIf(included_request AND event = {http_event} AND {current_period}) AS server_requests,
    countIf(included_request AND event IN {navigation_events} AND {current_period}) AS client_navigations,
    countIf(included_request AND event = {http_event} AND status > 0 AND {current_period}) AS status_observed,
    countIf(included_request AND event = {http_event} AND status >= 400 AND status < 500 AND {current_period}) AS client_errors,
    uniqIf(distinct_id, included_request AND {previous_period}) AS active_clients_prev,
    countIf(included_request AND event = {http_event} AND {previous_period}) AS server_requests_prev,
    countIf(included_request AND event IN {navigation_events} AND {previous_period}) AS client_navigations_prev,
    countIf(included_request AND event = {http_event} AND status >= 400 AND status < 500 AND {previous_period}) AS client_errors_prev,
    countIf(included_request AND event = {http_event} AND malformed_path AND {current_period}) AS malformed,
    countIf(included_request AND event = {http_event} AND malformed_path AND {previous_period}) AS malformed_prev,
    countIf(included_request AND event = {http_event} AND llms_source AND status = 200 AND {current_period}) AS llms_txt_fetches,
    countIf(agent_scope AND event = {http_event} AND (scanner_path OR static_asset_path) AND {current_period}) AS excluded_requests
FROM (
    SELECT
        distinct_id,
        event,
        timestamp,
        `$virt_bot_name` AS bot_name,
        properties.$host AS host,
        properties.$pathname AS pathname,
        {status} AS status,
        {agent_scope} AS agent_scope,
        {scanner_path} AS scanner_path,
        {static_asset} AS static_asset_path,
        {malformed_path} AS malformed_path,
        {llms_source_event} AS llms_source,
        {included_request} AS included_request
    FROM events
    WHERE and(event IN {agent_events}, `$virt_is_bot` = true, {periods}, {all_properties})
)
"""

CONVERSION_GOAL_QUERY = r"""
SELECT
    countIf(
        arrayExists(
            agent_request -> arrayExists(
                goal -> tupleElement(goal, 1) >= tupleElement(agent_request, 1)
                    AND dateDiff('second', tupleElement(agent_request, 1), tupleElement(goal, 1)) <= {conversion_window_seconds}
                    AND (
                        empty(tupleElement(agent_request, 2))
                        OR empty(tupleElement(goal, 2))
                        OR tupleElement(agent_request, 2) = tupleElement(goal, 2)
                    ),
                current_goal_events
            ),
            current_agent_events
        )
    ) AS converted_agents,
    countIf(
        arrayExists(
            agent_request -> arrayExists(
                goal -> tupleElement(goal, 1) >= tupleElement(agent_request, 1)
                    AND dateDiff('second', tupleElement(agent_request, 1), tupleElement(goal, 1)) <= {conversion_window_seconds}
                    AND (
                        empty(tupleElement(agent_request, 2))
                        OR empty(tupleElement(goal, 2))
                        OR tupleElement(agent_request, 2) = tupleElement(goal, 2)
                    ),
                previous_goal_events
            ),
            previous_agent_events
        )
    ) AS converted_agents_prev
FROM (
    SELECT
        distinct_id,
        groupArrayIf(
            tuple(timestamp, coalesce(toString(`$session_id`), '')),
            event IN {agent_events}
            AND `$virt_is_bot` = true
            AND {agent_scope}
            AND {included_path}
            AND {all_properties}
            AND {current_period}
        ) AS current_agent_events,
        groupArrayIf(
            tuple(timestamp, coalesce(toString(`$session_id`), '')),
            event IN {agent_events}
            AND `$virt_is_bot` = true
            AND {agent_scope}
            AND {included_path}
            AND {all_properties}
            AND {previous_period}
        ) AS previous_agent_events,
        groupArrayIf(
            tuple(timestamp, coalesce(toString(`$session_id`), '')),
            {conversion_goal} AND {current_period}
        ) AS current_goal_events,
        groupArrayIf(
            tuple(timestamp, coalesce(toString(`$session_id`), '')),
            {conversion_goal} AND {previous_period}
        ) AS previous_goal_events
    FROM events
    WHERE and(
        {periods},
        (
            (
                event IN {agent_events}
                AND `$virt_is_bot` = true
                AND {agent_scope}
                AND {included_path}
                AND {all_properties}
            )
            OR {conversion_goal}
        )
    )
    GROUP BY distinct_id
)
"""

DOUBLE_FETCH_QUERY = r"""
SELECT
    sum(arrayCount(
        md_time -> arrayExists(
            html_time -> abs(dateDiff('second', md_time, html_time)) <= {navigation_window_seconds},
            current_html_times
        ),
        current_md_times
    )) AS wasted,
    sum(arrayCount(
        md_time -> arrayExists(
            html_time -> abs(dateDiff('second', md_time, html_time)) <= {navigation_window_seconds},
            previous_html_times
        ),
        previous_md_times
    )) AS wasted_prev,
    uniqIf(
        page,
        arrayExists(
            md_time -> arrayExists(
                html_time -> abs(dateDiff('second', md_time, html_time)) <= {navigation_window_seconds},
                current_html_times
            ),
            current_md_times
        )
    ) AS waste_pages
FROM (
    SELECT
        distinct_id,
        coalesce(toString(`$session_id`), '') AS session_id,
        {page_key} AS page,
        groupArrayIf(timestamp, {is_md_twin} AND {current_period}) AS current_md_times,
        groupArrayIf(timestamp, {is_200} AND NOT ({is_md}) AND {current_period}) AS current_html_times,
        groupArrayIf(timestamp, {is_md_twin} AND {previous_period}) AS previous_md_times,
        groupArrayIf(timestamp, {is_200} AND NOT ({is_md}) AND {previous_period}) AS previous_html_times
    FROM events
    WHERE and(
        event = {http_event},
        `$virt_is_bot` = true,
        {agent_scope},
        {included_path},
        {periods},
        {all_properties}
    )
    GROUP BY distinct_id, session_id, page
)
"""

ISSUES_QUERY = r"""
SELECT
    {intent_key} AS intent_key,
    any({normalized_path}) AS intent_path,
    countIf({current_period}) AS demand,
    countIf({previous_period}) AS demand_prev,
    uniqIf(properties.$pathname, {current_period}) AS variants,
    argMaxIf(`$virt_bot_name`, timestamp, {current_period}) AS top_agent,
    minIf(timestamp, {current_period}) AS first_seen,
    maxIf(timestamp, {current_period}) AS last_seen
FROM events
WHERE and(
    event = {http_event},
    `$virt_is_bot` = true,
    {agent_scope},
    {is_404},
    {included_path},
    {periods},
    {all_properties}
)
GROUP BY intent_key
ORDER BY demand DESC, intent_key
LIMIT {fetch_limit}
OFFSET {offset}
"""

PAGE_REQUESTS_QUERY = r"""
SELECT
    page,
    sum(fetches) AS fetches,
    sum(md_fetches) AS md_fetches,
    sum(html_fetches) AS html_fetches,
    countIf(client_paired) AS paired_clients
FROM (
    SELECT
        distinct_id,
        page,
        sum(session_fetches) AS fetches,
        sum(session_md_fetches) AS md_fetches,
        sum(session_html_fetches) AS html_fetches,
        max(session_paired) AS client_paired
    FROM (
        SELECT
            distinct_id,
            coalesce(toString(`$session_id`), '') AS session_id,
            {page_key} AS page,
            count() AS session_fetches,
            countIf({is_md}) AS session_md_fetches,
            countIf(NOT ({is_md})) AS session_html_fetches,
            arrayExists(
                md_time -> arrayExists(
                    html_time -> abs(dateDiff('second', md_time, html_time)) <= {navigation_window_seconds},
                    groupArrayIf(timestamp, NOT ({is_md}))
                ),
                groupArrayIf(timestamp, {is_md})
            ) AS session_paired
        FROM events
        WHERE and(
            event = {http_event},
            `$virt_is_bot` = true,
            {agent_scope},
            {is_200},
            {included_path},
            {current_period},
            {all_properties}
        )
        GROUP BY distinct_id, session_id, page
    )
    GROUP BY distinct_id, page
)
GROUP BY page
ORDER BY fetches DESC, page
LIMIT {fetch_limit}
OFFSET {offset}
"""

DEMAND_QUERY = r"""
SELECT
    concat(coalesce(properties.$host, ''), properties.$pathname) AS page,
    coalesce(properties.$host, '') AS host,
    properties.$pathname AS path,
    count() AS demand
FROM events
WHERE and(
    event = {http_event},
    `$virt_is_bot` = true,
    {agent_scope},
    {is_200},
    {included_path},
    {current_period},
    {all_properties}
)
GROUP BY page, host, path
ORDER BY demand DESC, page
LIMIT {fetch_limit}
OFFSET {offset}
"""

ISSUE_VARIANTS_QUERY = r"""
SELECT
    properties.$pathname AS variant,
    count() AS demand,
    arrayElement(topK(1)(`$virt_bot_name`), 1) AS top_agent,
    min(timestamp) AS first_seen
FROM events
WHERE and(
    event = {http_event},
    `$virt_is_bot` = true,
    {agent_scope},
    {is_404},
    {included_path},
    {intent_key} = {selected_intent_key},
    {current_period},
    {all_properties}
)
GROUP BY variant
ORDER BY demand DESC, variant
LIMIT {fetch_limit}
OFFSET {offset}
"""


REQUEST_ANATOMY_QUERY = r"""
SELECT
    agent,
    sum(page_requests) AS requests,
    sum(page_accept_captured) AS accept_captured,
    sum(page_accept_markdown_preferred) AS accept_markdown_preferred,
    sum(page_accept_markdown_accepted) AS accept_markdown_accepted,
    sum(page_accept_html_only) AS accept_html_only,
    sum(page_requested_markdown) AS requested_markdown,
    sum(page_served_captured) AS served_captured,
    sum(page_served_markdown) AS served_markdown,
    sum(page_retry_pairs) AS retry_pairs,
    sum(page_errors) AS errors
FROM (
    SELECT
        agent,
        distinct_id,
        session_id,
        page,
        count() AS page_requests,
        countIf(accept_class != 'unknown') AS page_accept_captured,
        countIf(accept_class = 'markdown_preferred') AS page_accept_markdown_preferred,
        countIf(accept_class = 'markdown_accepted') AS page_accept_markdown_accepted,
        countIf(accept_class = 'html_only') AS page_accept_html_only,
        countIf(is_md) AS page_requested_markdown,
        countIf(content_type != '') AS page_served_captured,
        countIf(served_markdown) AS page_served_markdown,
        countIf(is_error) AS page_errors,
        arrayCount(
            md_time -> arrayExists(
                html_time -> dateDiff('second', html_time, md_time) >= 0
                    AND dateDiff('second', html_time, md_time) <= {navigation_window_seconds},
                groupArrayIf(timestamp, is_200 AND NOT is_md)
            ),
            groupArrayIf(timestamp, is_md)
        ) AS page_retry_pairs
    FROM (
        SELECT
            `$virt_bot_name` AS agent,
            distinct_id,
            {session_id} AS session_id,
            timestamp,
            {page_key} AS page,
            {status} AS status,
            {is_md} AS is_md,
            {is_200} AS is_200,
            {status} >= 400 AS is_error,
            {accept_class} AS accept_class,
            {content_type} AS content_type,
            {served_markdown} AS served_markdown
        FROM events
        WHERE and(
            event = {http_event},
            `$virt_is_bot` = true,
            {agent_scope},
            {included_path},
            {current_period},
            {all_properties}
        )
    )
    GROUP BY agent, distinct_id, session_id, page
)
GROUP BY agent
HAVING requests >= {minimum_requests}
ORDER BY requests DESC, agent
LIMIT {fetch_limit}
OFFSET {offset}
"""

_SESSIONIZED_EVENTS = r"""
SELECT
    distinct_id,
    uuid,
    timestamp,
    agent,
    host,
    path,
    status,
    is_md,
    is_200,
    is_error,
    referrer,
    session_id,
    multiIf(
        session_id != '',
        concat('s:', distinct_id, ':', agent, ':', host, ':', session_id),
        concat(
            'i:', distinct_id, ':', agent, ':', host, ':',
            toString(sum(new_journey) OVER (
                PARTITION BY distinct_id, agent, host
                ORDER BY timestamp ASC, uuid ASC
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ))
        )
    ) AS journey_key
FROM (
    SELECT
        distinct_id,
        uuid,
        timestamp,
        agent,
        host,
        path,
        status,
        is_md,
        is_200,
        is_error,
        referrer,
        session_id,
        if(
            dateDiff('second', lagInFrame(timestamp) OVER (
                PARTITION BY distinct_id, agent, host
                ORDER BY timestamp ASC, uuid ASC
                ROWS BETWEEN 1 PRECEDING AND CURRENT ROW
            ), timestamp) > {inactivity_window_seconds},
            1,
            0
        ) AS new_journey
    FROM (
        SELECT
            distinct_id,
            uuid,
            timestamp,
            `$virt_bot_name` AS agent,
            coalesce(properties.$host, '') AS host,
            properties.$pathname AS path,
            {status} AS status,
            {is_md} AS is_md,
            {is_200} AS is_200,
            {status} >= 400 AS is_error,
            {referrer} AS referrer,
            {session_id} AS session_id
        FROM events
        WHERE and(
            event = {http_event},
            `$virt_is_bot` = true,
            {agent_scope},
            {included_path},
            {current_period},
            {all_properties}
        )
    )
)
"""

TRANSITIONS_QUERY = (
    r"""
SELECT next_path, count() AS requests, countIf(next_status = 404) AS not_found
FROM (
    SELECT
        timestamp,
        path,
        host,
        leadInFrame(path) OVER (
            PARTITION BY journey_key
            ORDER BY timestamp ASC, uuid ASC
            ROWS BETWEEN CURRENT ROW AND 1 FOLLOWING
        ) AS next_path,
        leadInFrame(timestamp) OVER (
            PARTITION BY journey_key
            ORDER BY timestamp ASC, uuid ASC
            ROWS BETWEEN CURRENT ROW AND 1 FOLLOWING
        ) AS next_timestamp,
        leadInFrame(status) OVER (
            PARTITION BY journey_key
            ORDER BY timestamp ASC, uuid ASC
            ROWS BETWEEN CURRENT ROW AND 1 FOLLOWING
        ) AS next_status
    FROM (
"""
    + _SESSIONIZED_EVENTS
    + r"""
    )
)
WHERE and(
    {llms_source_row},
    next_path != '',
    next_path != path,
    next_timestamp >= timestamp,
    dateDiff('second', timestamp, next_timestamp) <= {navigation_window_seconds}
)
GROUP BY next_path
ORDER BY requests DESC, next_path
LIMIT {fetch_limit}
OFFSET {offset}
"""
)

JOURNEY_SUMMARY_QUERY = (
    r"""
SELECT
    count() AS total_journeys,
    round(quantile(0.5)(pages)) AS median_pages,
    round(quantile(0.5)(requests)) AS median_requests,
    round(quantile(0.5)(duration_seconds)) AS median_duration_seconds,
    countIf(errors > 0) AS journeys_with_errors,
    countIf(is_explicit) AS explicit_journeys
FROM (
    SELECT
        journey_key,
        uniq(path) AS pages,
        count() AS requests,
        dateDiff('second', min(timestamp), max(timestamp)) AS duration_seconds,
        countIf(is_error) AS errors,
        max(session_id != '') AS is_explicit
    FROM (
"""
    + _SESSIONIZED_EVENTS
    + r"""
    )
    GROUP BY journey_key
)
"""
)

JOURNEYS_QUERY = (
    r"""
SELECT
    journey_key,
    min(timestamp) AS started,
    arrayElement(topK(1)(agent), 1) AS agent,
    any(host) AS host,
    uniq(path) AS pages,
    count() AS requests,
    dateDiff('second', min(timestamp), max(timestamp)) AS duration_seconds,
    countIf(is_error) AS errors,
    if(max(session_id != ''), 'explicit', 'inferred') AS confidence
FROM (
"""
    + _SESSIONIZED_EVENTS
    + r"""
)
GROUP BY journey_key
ORDER BY started DESC, journey_key
LIMIT {fetch_limit}
OFFSET {offset}
"""
)

JOURNEY_DETAIL_QUERY = (
    r"""
SELECT
    timestamp,
    path,
    status,
    if(is_md, 'markdown', 'html') AS format,
    referrer,
    multiIf(
        prev_path = '', 'start',
        timestamp = prev_timestamp, 'parallel',
        referrer != '' AND path(referrer) = prev_path, 'confirmed',
        'sequential'
    ) AS transition
FROM (
    SELECT
        timestamp,
        path,
        status,
        is_md,
        referrer,
        lagInFrame(path, 1, '') OVER (
            PARTITION BY journey_key
            ORDER BY timestamp ASC, uuid ASC
            ROWS BETWEEN 1 PRECEDING AND CURRENT ROW
        ) AS prev_path,
        lagInFrame(timestamp) OVER (
            PARTITION BY journey_key
            ORDER BY timestamp ASC, uuid ASC
            ROWS BETWEEN 1 PRECEDING AND CURRENT ROW
        ) AS prev_timestamp
    FROM (
"""
    + _SESSIONIZED_EVENTS
    + r"""
    )
    WHERE journey_key = {selected_journey_key}
)
ORDER BY timestamp ASC
LIMIT {journey_step_limit}
"""
)


def _merge_hogql(*parts: str | None) -> str | None:
    return "\n\n".join(part for part in parts if part) or None


class WebAgentAnalyticsQueryRunner(WebAnalyticsQueryRunner[WebAgentAnalyticsQueryResponse]):
    query: WebAgentAnalyticsQuery
    cached_response: CachedWebAgentAnalyticsQueryResponse

    def _agent_categories(self) -> ast.Tuple:
        categories = AGENT_CATEGORIES_WITH_CRAWLERS if self.query.includeCrawlers else AGENT_CATEGORIES
        return ast.Tuple(exprs=[ast.Constant(value=category) for category in categories])

    def _result_limit(self) -> int:
        return bounded_int(self.query.limit, default=DEFAULT_RESULT_LIMIT, minimum=1, maximum=MAX_RESULT_LIMIT)

    def _offset(self) -> int:
        return max(self.query.offset or 0, 0)

    def _llms_source_expr(self, *, host: str, path: str) -> ast.Expr:
        parsed_url = urlparse.urlparse(self.query.llmsTxtUrl or "")
        source_path = parsed_url.path or "/llms.txt"
        source_host = (parsed_url.hostname or "").lower()
        path_expr = parse_expr(
            "{path} = {source_path}",
            placeholders={"path": parse_expr(path), "source_path": ast.Constant(value=source_path)},
        )
        if not source_host:
            return path_expr
        return parse_expr(
            "{path_match} AND lower(coalesce({host}, '')) = {source_host}",
            placeholders={
                "path_match": path_expr,
                "host": parse_expr(host),
                "source_host": ast.Constant(value=source_host),
            },
        )

    @cached_property
    def _placeholders(self) -> dict[str, ast.Expr]:
        status = response_status_code_expr()
        is_200 = parse_expr("{status} = 200", placeholders={"status": status})
        is_404 = parse_expr("{status} = 404", placeholders={"status": status})
        is_md = markdown_path_expr()
        scanner_path = scanner_path_expr()
        is_asset = static_asset_expr()
        normalized_path = (
            normalized_path_expr()
            if self.query.contentGrouping != WebAgentContentGrouping.EXACT
            else ast.Field(chain=["properties", "$pathname"])
        )
        intent_key = parse_expr(
            "concat(coalesce(properties.$host, ''), {normalized_path})",
            placeholders={"normalized_path": normalized_path},
        )
        agent_scope = parse_expr(
            "`$virt_traffic_category` IN {agent_categories}",
            placeholders={"agent_categories": self._agent_categories()},
        )
        included_request = (
            agent_scope
            if self.query.includeExcluded
            else ast.And(exprs=[agent_scope, ast.Not(expr=scanner_path), ast.Not(expr=is_asset)])
        )
        included_path = (
            ast.Constant(value=True)
            if self.query.includeExcluded
            else ast.And(exprs=[ast.Not(expr=scanner_path), ast.Not(expr=is_asset)])
        )
        navigation_window_minutes = bounded_int(
            self.query.navigationWindowMinutes,
            default=DEFAULT_NAVIGATION_WINDOW_MINUTES,
            minimum=1,
            maximum=MAX_NAVIGATION_WINDOW_MINUTES,
        )
        inactivity_window_minutes = bounded_int(
            self.query.inactivityWindowMinutes,
            default=DEFAULT_INACTIVITY_WINDOW_MINUTES,
            minimum=1,
            maximum=MAX_INACTIVITY_WINDOW_MINUTES,
        )
        conversion_window_hours = bounded_int(
            self.query.conversionWindowHours,
            default=DEFAULT_CONVERSION_WINDOW_HOURS,
            minimum=1,
            maximum=MAX_CONVERSION_WINDOW_HOURS,
        )
        minimum_requests = bounded_int(
            self.query.minimumRequests,
            default=DEFAULT_MINIMUM_REQUESTS,
            minimum=1,
            maximum=MAX_MINIMUM_REQUESTS,
        )

        return {
            "agent_events": ast.Tuple(exprs=[ast.Constant(value=event) for event in AGENT_EVENTS]),
            "navigation_events": ast.Tuple(exprs=[ast.Constant(value=event) for event in AGENT_NAVIGATION_EVENTS]),
            "http_event": ast.Constant(value=AGENT_HTTP_EVENT),
            "agent_scope": agent_scope,
            "current_period": self._current_period_expression("timestamp"),
            "previous_period": self._previous_period_expression("timestamp"),
            "periods": self._periods_expression("timestamp"),
            "all_properties": self.all_properties(),
            "status": status,
            "is_404": is_404,
            "is_200": is_200,
            "is_md": is_md,
            "is_md_twin": parse_expr("{is_200} AND {is_md}", placeholders={"is_200": is_200, "is_md": is_md}),
            "scanner_path": scanner_path,
            "static_asset": is_asset,
            "malformed_path": malformed_path_expr(),
            "included_request": included_request,
            "included_path": included_path,
            "normalized_path": normalized_path,
            "page_key": page_identity_expr(),
            "intent_key": intent_key,
            "selected_intent_key": ast.Constant(value=self.query.intentKey or ""),
            "conversion_goal": self.conversion_goal_expr or ast.Constant(value=False),
            "conversion_window_seconds": ast.Constant(value=conversion_window_hours * 60 * 60),
            "navigation_window_seconds": ast.Constant(value=navigation_window_minutes * 60),
            "inactivity_window_seconds": ast.Constant(value=inactivity_window_minutes * 60),
            "minimum_requests": ast.Constant(value=minimum_requests),
            "fetch_limit": ast.Constant(value=self._result_limit() + 1),
            "journey_step_limit": ast.Constant(value=min(self._result_limit(), MAX_JOURNEY_STEPS)),
            "offset": ast.Constant(value=self._offset()),
            "accept_class": accept_class_expr(),
            "content_type": response_content_type_expr(),
            "served_markdown": served_markdown_expr(),
            "referrer": referrer_expr(),
            "session_id": agent_session_id_expr(),
            "selected_journey_key": ast.Constant(value=self.query.journeyKey or ""),
            "llms_source_event": self._llms_source_expr(host="properties.$host", path="properties.$pathname"),
            "llms_source_row": self._llms_source_expr(host="host", path="path"),
        }

    def to_query(self) -> ast.SelectQuery:
        if self.query.queryType == WebAgentAnalyticsQueryType.ISSUE_VARIANTS and not self.query.intentKey:
            raise QueryError("intentKey is required for issue variants")
        if self.query.queryType == WebAgentAnalyticsQueryType.JOURNEY_DETAIL and not self.query.journeyKey:
            raise QueryError("journeyKey is required for journey detail")

        templates = {
            WebAgentAnalyticsQueryType.OVERVIEW: OVERVIEW_QUERY,
            WebAgentAnalyticsQueryType.ISSUES: ISSUES_QUERY,
            WebAgentAnalyticsQueryType.PAGE_REQUESTS: PAGE_REQUESTS_QUERY,
            WebAgentAnalyticsQueryType.TRANSITIONS: TRANSITIONS_QUERY,
            WebAgentAnalyticsQueryType.DEMAND: DEMAND_QUERY,
            WebAgentAnalyticsQueryType.ISSUE_VARIANTS: ISSUE_VARIANTS_QUERY,
            WebAgentAnalyticsQueryType.REQUEST_ANATOMY: REQUEST_ANATOMY_QUERY,
            WebAgentAnalyticsQueryType.JOURNEY_SUMMARY: JOURNEY_SUMMARY_QUERY,
            WebAgentAnalyticsQueryType.JOURNEYS: JOURNEYS_QUERY,
            WebAgentAnalyticsQueryType.JOURNEY_DETAIL: JOURNEY_DETAIL_QUERY,
        }
        with self.timings.measure("web_agent_analytics_query"):
            query = parse_select(templates[self.query.queryType], timings=self.timings, placeholders=self._placeholders)
        assert isinstance(query, ast.SelectQuery)
        return query

    def _execute(self, query: ast.SelectQuery, query_type: str) -> HogQLQueryResponse:
        return execute_hogql_query(
            query_type=query_type,
            query=query,
            team=self.team,
            user=self.user,
            timings=self.timings,
            modifiers=self.modifiers,
            limit_context=self.limit_context,
        )

    def _calculate(self) -> WebAgentAnalyticsQueryResponse:
        response = self._execute(self.to_query(), f"web_agent_analytics_{self.query.queryType.value}")
        columns = list(response.columns or [])
        results = [list(row) for row in response.results]
        types = list(response.types or [])
        hogql = response.hogql
        result_limit = self._result_limit()
        paginates = self.query.queryType not in (
            WebAgentAnalyticsQueryType.OVERVIEW,
            WebAgentAnalyticsQueryType.JOURNEY_SUMMARY,
            WebAgentAnalyticsQueryType.JOURNEY_DETAIL,
        )
        has_more = paginates and len(results) > result_limit
        if has_more:
            results = results[:result_limit]

        if self.query.queryType == WebAgentAnalyticsQueryType.OVERVIEW:
            waste_query = parse_select(DOUBLE_FETCH_QUERY, timings=self.timings, placeholders=self._placeholders)
            assert isinstance(waste_query, ast.SelectQuery)
            waste_response = self._execute(waste_query, "web_agent_analytics_double_fetch")
            columns.extend(waste_response.columns or [])
            types.extend(waste_response.types or [])
            summary_row = results[0] if results else []
            waste_row = list(waste_response.results[0]) if waste_response.results else []
            results = [summary_row + waste_row]
            hogql = _merge_hogql(hogql, waste_response.hogql)

            if self.query.conversionGoal:
                conversion_query = parse_select(
                    CONVERSION_GOAL_QUERY, timings=self.timings, placeholders=self._placeholders
                )
                assert isinstance(conversion_query, ast.SelectQuery)
                conversion_response = self._execute(conversion_query, "web_agent_analytics_conversion_goal")
                columns.extend(conversion_response.columns or [])
                types.extend(conversion_response.types or [])
                conversion_row = list(conversion_response.results[0]) if conversion_response.results else []
                results = [[*results[0], *conversion_row]]
                hogql = _merge_hogql(hogql, conversion_response.hogql)

        return WebAgentAnalyticsQueryResponse(
            columns=columns,
            results=results,
            timings=response.timings,
            types=types,
            hogql=hogql,
            modifiers=self.modifiers,
            hasMore=has_more,
            limit=result_limit,
            offset=self._offset(),
            definitionVersion=AGENT_ANALYTICS_DEFINITION_VERSION,
        )
