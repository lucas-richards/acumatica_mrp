from django.db import migrations

ROLE_GROUPS = ('viewer', 'planner')


def create_roles_and_settings(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    for name in ROLE_GROUPS:
        Group.objects.get_or_create(name=name)
    PlanSettings = apps.get_model('mrp', 'PlanSettings')
    PlanSettings.objects.get_or_create(pk=1)


def remove_roles(apps, schema_editor):
    apps.get_model('auth', 'Group').objects.filter(name__in=ROLE_GROUPS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        ('mrp', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(create_roles_and_settings, remove_roles),
    ]
