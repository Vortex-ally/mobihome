from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("properties", "0019_authsession")]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="google_sub",
            field=models.CharField(
                blank=True, max_length=64, null=True, unique=True, verbose_name="Google ID (sub)"
            ),
        ),
    ]
