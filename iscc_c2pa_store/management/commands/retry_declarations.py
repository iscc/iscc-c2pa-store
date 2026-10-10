"""Retry pending declarations and unfinished withdrawals once; the container entrypoint runs it in a loop."""

from django.core.management.base import BaseCommand

from iscc_c2pa_store import store


class Command(BaseCommand):
    """`python manage.py retry_declarations`."""

    help = "Retry pending ISCC declarations and unfinished withdrawals"

    def handle(self, *args, **options):
        """Run one round of retries and report how many declarations were tried."""
        tried = store.retry_declarations()
        if tried:
            self.stdout.write(f"Retried {tried} declaration(s)")
