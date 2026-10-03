"""No-op step: exercises the chain; applies to nothing real."""
from lib.migrate.runner import Step

STEP = Step("00_noop", 1, 2, detect=lambda ctx: False,
            apply=lambda ctx: None, verify=lambda ctx: None)
