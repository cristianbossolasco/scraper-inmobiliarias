from getpass import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Crea un usuario sin privilegios para acceder al Radar movil."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="radar-mobile")
        parser.add_argument("--email", default="")

    def handle(self, *args, **options):
        username = (options["username"] or "").strip()
        email = (options["email"] or "").strip()
        if not username:
            raise CommandError("El nombre de usuario no puede estar vacio.")

        user_model = get_user_model()
        if user_model.objects.filter(username=username).exists():
            raise CommandError(
                f"El usuario {username!r} ya existe. Elegi otro nombre o cambiale la clave desde Django."
            )

        password = getpass("Contraseña móvil: ")
        confirmation = getpass("Repetir contraseña: ")
        if password != confirmation:
            raise CommandError("Las contraseñas no coinciden.")

        candidate = user_model(username=username, email=email)
        try:
            validate_password(password, user=candidate)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc

        user = user_model.objects.create_user(
            username=username,
            email=email,
            password=password,
            is_staff=False,
            is_superuser=False,
            is_active=True,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Usuario móvil {user.username!r} creado sin permisos administrativos."
            )
        )

