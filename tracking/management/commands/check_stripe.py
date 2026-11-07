"""python manage.py check_stripe — confirm the Stripe keys work before customers find out they don't."""
from django.core.management.base import BaseCommand, CommandError

from tracking import stripe_gateway


class Command(BaseCommand):
    help = "Check that TP_STRIPE_SECRET_KEY / TP_STRIPE_PUBLISHABLE_KEY are valid by making one read-only Stripe call."

    def handle(self, *args, **options):
        problem = stripe_gateway.config_problem()
        if problem:
            raise CommandError(problem)
        try:
            mode = stripe_gateway.check_connection()
        except stripe_gateway.GatewayError as e:
            raise CommandError(f"Stripe rejected the request: {e} (see the log line above for Stripe's reason)")
        self.stdout.write(self.style.SUCCESS(f"Stripe keys OK ({mode})."))
