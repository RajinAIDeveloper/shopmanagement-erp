import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections, OperationalError
from store.services import expire_due_reservations


class Command(BaseCommand):
    help='Release overdue unconfirmed online reservations; --watch runs every 60 seconds.'
    def add_arguments(self,parser): parser.add_argument('--watch',action='store_true')
    def handle(self,*args,**options):
        try:
            while True:
                close_old_connections()
                try:
                    total=0
                    while True:
                        count=expire_due_reservations()
                        total+=count
                        if count<500: break
                    if total or not options['watch']: self.stdout.write(f'Expired {total} reservations.')
                except OperationalError:
                    if not options['watch']: raise
                    self.stderr.write('Database busy; retrying on the next interval.')
                if not options['watch']: break
                time.sleep(60)
        except KeyboardInterrupt:
            self.stdout.write('Reservation worker stopped.')
