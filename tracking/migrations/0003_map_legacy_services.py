from django.db import migrations

LEGACY = {"Standard": "Economy Select", "Economy": "Economy Select"}


def forwards(apps, schema_editor):
    for model in ("Shipment", "QuoteRequest"):
        M = apps.get_model("tracking", model)
        for old, new in LEGACY.items():
            M.objects.filter(service_type=old).update(service_type=new)


class Migration(migrations.Migration):
    dependencies = [("tracking", "0002_express_products_bookings")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
