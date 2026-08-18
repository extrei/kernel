"""Local kernel primitives for projects."""

from .blueprint import (
    BlueprintError,
    BlueprintRecord,
    blueprint,
    check_blueprint,
    meta_schema,
    set_blueprint,
)
from .circuit import CircuitVerdict, circuit, events, schedule
from .contracts import (
    ContractError,
    UnauthorizedWriteError,
    audit_contracts,
    authorize,
    contracts,
)
from .controller import StepError, StepRecord, read_artifact, record_step
from .jsonpatch import PatchError, touched_paths
from .kernel import (
    InitResult,
    LedgerIntegrityError,
    StateTreeError,
    entries,
    initialize,
    verify,
)
from .state import PatchRecord, StaleParentError, apply_patch, record_failure, state
from .schema import (
    SchemaError,
    audit_schema,
    collection,
    schema,
    validate,
)
from .views import (
    StaleViewError,
    ViewError,
    ViewRecord,
    audit_views,
    derive_view,
    view,
)

__all__ = [
    "BlueprintError",
    "BlueprintRecord",
    "CircuitVerdict",
    "ContractError",
    "InitResult",
    "LedgerIntegrityError",
    "PatchError",
    "PatchRecord",
    "SchemaError",
    "StaleParentError",
    "StaleViewError",
    "StateTreeError",
    "StepError",
    "StepRecord",
    "UnauthorizedWriteError",
    "ViewError",
    "ViewRecord",
    "apply_patch",
    "audit_contracts",
    "audit_schema",
    "audit_views",
    "authorize",
    "blueprint",
    "check_blueprint",
    "circuit",
    "collection",
    "contracts",
    "derive_view",
    "entries",
    "events",
    "initialize",
    "meta_schema",
    "read_artifact",
    "record_failure",
    "record_step",
    "schedule",
    "schema",
    "set_blueprint",
    "state",
    "touched_paths",
    "validate",
    "verify",
    "view",
]
