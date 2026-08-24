from posthog.hogql import ast
from posthog.hogql.parser import parse_expr

AGENT_ANALYTICS_DEFINITION_VERSION = 3

AGENT_HTTP_EVENT = "$http_log"
AGENT_NAVIGATION_EVENTS = ("$pageview", "$screen")
AGENT_EVENTS = (*AGENT_NAVIGATION_EVENTS, AGENT_HTTP_EVENT)

AGENT_CATEGORIES = ("ai_assistant", "ai_search")
AGENT_CATEGORIES_WITH_CRAWLERS = (*AGENT_CATEGORIES, "ai_crawler")

DEFAULT_NAVIGATION_WINDOW_MINUTES = 30
MAX_NAVIGATION_WINDOW_MINUTES = 24 * 60
DEFAULT_INACTIVITY_WINDOW_MINUTES = 30
MAX_INACTIVITY_WINDOW_MINUTES = 24 * 60
DEFAULT_CONVERSION_WINDOW_HOURS = 24
MAX_CONVERSION_WINDOW_HOURS = 30 * 24
DEFAULT_MINIMUM_REQUESTS = 20
MAX_MINIMUM_REQUESTS = 10_000
DEFAULT_RESULT_LIMIT = 100
MAX_RESULT_LIMIT = 500
MAX_JOURNEY_STEPS = 200


def bounded_int(value: int | None, *, default: int, minimum: int, maximum: int) -> int:
    return min(max(value if value is not None else default, minimum), maximum)


def scanner_path_expr() -> ast.Expr:
    return parse_expr(
        """
properties.$pathname ILIKE '%/.env%'
    OR properties.$pathname ILIKE '%/.git%'
    OR properties.$pathname ILIKE '%/.aws%'
    OR properties.$pathname ILIKE '%/.svn%'
    OR properties.$pathname ILIKE '%wp-login%'
    OR properties.$pathname ILIKE '%wp-admin%'
    OR properties.$pathname ILIKE '%phpmyadmin%'
    OR properties.$pathname ILIKE '%/vendor/phpunit%'
    OR properties.$pathname ILIKE '%/.well-known/security%'
"""
    )


def static_asset_expr() -> ast.Expr:
    return parse_expr(
        """
properties.$pathname ILIKE '%.ico'
    OR properties.$pathname ILIKE '%.png'
    OR properties.$pathname ILIKE '%.jpg'
    OR properties.$pathname ILIKE '%.jpeg'
    OR properties.$pathname ILIKE '%.gif'
    OR properties.$pathname ILIKE '%.svg'
    OR properties.$pathname ILIKE '%.webp'
    OR properties.$pathname ILIKE '%.css'
    OR properties.$pathname ILIKE '%.js'
    OR properties.$pathname ILIKE '%.map'
    OR properties.$pathname ILIKE '%.woff%'
    OR properties.$pathname ILIKE '%/apple-touch-icon%'
    OR properties.$pathname = '/robots.txt'
    OR properties.$pathname = '/sitemap.xml'
"""
    )


def malformed_path_expr() -> ast.Expr:
    return parse_expr(
        """
properties.$pathname ILIKE '%/null/%'
    OR properties.$pathname ILIKE '%/undefined/%'
    OR endsWith(properties.$pathname, '/null')
    OR endsWith(properties.$pathname, '/undefined')
"""
    )


def normalized_path_expr() -> ast.Expr:
    return parse_expr(
        r"""
replaceRegexpAll(
    replaceRegexpAll(
        replaceRegexpAll(properties.$pathname, '\\.(md|html?|json|txt|xml|ya?ml)$', ''),
        '[-/]v?[0-9]+\\.[0-9]+(\\.[0-9]+)?',
        ''
    ),
    '/+$',
    ''
)
"""
    )


def markdown_path_expr() -> ast.Expr:
    return parse_expr("properties.$pathname ILIKE '%.md'")


def page_identity_expr() -> ast.Expr:
    return parse_expr(
        r"concat(coalesce(properties.$host, ''), replaceRegexpAll(properties.$pathname, '(?i)\\.md$', ''))"
    )


def referrer_expr() -> ast.Expr:
    return parse_expr(
        "coalesce(nullIf(toString(properties.$referrer), ''), nullIf(toString(properties.proxy_referer), ''), '')"
    )


def response_status_code_expr() -> ast.Expr:
    return parse_expr(
        "toInt(coalesce(nullIf(toString(properties.$http_response_status_code), ''), nullIf(toString(properties.proxy_status_code), ''), '0'))"
    )


def agent_session_id_expr() -> ast.Expr:
    return parse_expr(
        "coalesce(nullIf(nullIf(toString(properties.$agent_session_id), ''), 'null'), nullIf(nullIf(toString(properties.$session_id), ''), 'null'), '')"
    )


def accept_header_expr() -> ast.Expr:
    return parse_expr("lower(coalesce(toString(properties.$http_request_accept), ''))")


def _accept_range_quality_expr(media_range: str) -> ast.Expr:
    presence_pattern = rf"(?:^|,)\s*{media_range}\s*(?:;[^,]*)?(?:,|$)"
    quality_pattern = rf"(?:^|,)\s*{media_range}\s*(?:;[^,;]*)*;\s*q\s*=\s*([01](?:\.\d+)?)"
    quality = parse_expr(
        "extract({accept}, {quality_pattern})",
        placeholders={
            "accept": accept_header_expr(),
            "quality_pattern": ast.Constant(value=quality_pattern),
        },
    )
    return parse_expr(
        """
multiIf(
    NOT match({accept}, {presence_pattern}), -1.0,
    {quality} = '', 1.0,
    greatest(0.0, least(1.0, toFloatOrZero({quality})))
)
""",
        placeholders={
            "accept": accept_header_expr(),
            "presence_pattern": ast.Constant(value=presence_pattern),
            "quality": quality,
        },
    )


def _accepted_quality_expr(media_range: str, type_wildcard: str) -> ast.Expr:
    exact_quality = _accept_range_quality_expr(media_range)
    type_quality = _accept_range_quality_expr(type_wildcard)
    wildcard_quality = _accept_range_quality_expr(r"\*/\*")
    return parse_expr(
        "multiIf({exact} >= 0, {exact}, {type_wildcard} >= 0, {type_wildcard}, {wildcard} >= 0, {wildcard}, 0.0)",
        placeholders={
            "exact": exact_quality,
            "type_wildcard": type_quality,
            "wildcard": wildcard_quality,
        },
    )


def accept_class_expr() -> ast.Expr:
    markdown_quality = _accepted_quality_expr("text/markdown", r"text/\*")
    html_quality = _accepted_quality_expr("text/html", r"text/\*")
    return parse_expr(
        """
multiIf(
    {accept} = '', 'unknown',
    {markdown_quality} <= 0, 'html_only',
    {markdown_quality} > {html_quality}, 'markdown_preferred',
    'markdown_accepted'
)
""",
        placeholders={
            "accept": accept_header_expr(),
            "markdown_quality": markdown_quality,
            "html_quality": html_quality,
        },
    )


def response_content_type_expr() -> ast.Expr:
    return parse_expr("lower(coalesce(toString(properties.$http_response_content_type), ''))")


def served_markdown_expr() -> ast.Expr:
    return parse_expr(
        "{content_type} != '' AND position({content_type}, 'markdown') > 0",
        placeholders={"content_type": response_content_type_expr()},
    )
