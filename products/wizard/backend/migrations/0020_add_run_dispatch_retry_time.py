from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("wizard", "0019_add_wizard_worker_usage"),
    ]

    operations = [
        migrations.AddField(
            model_name="wizardrun",
            name="dispatch_next_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
