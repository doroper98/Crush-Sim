"""Allow ``python -m crushsim`` as an alternative to the ``csim`` script.

The console-script launcher some pip versions generate mishandles install
paths containing spaces on Windows; running the package as a module bypasses
the launcher entirely.
"""

from .cli import main

if __name__ == "__main__":
    main()
