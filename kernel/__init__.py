"""Local kernel primitives for projects."""

from .contracts import (
    ContractError,
    ContractRecord,
    UnauthorizedWriteError,
    audit_contracts,
    authorize,
    contracts,
    set_contracts,
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
from .state import PatchRecord, StaleParentError, apply_patch, state
from .schema import (
    SchemaError,
    SchemaRecord,
    audit_schema,
    collection,
    schema,
    set_schema,
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
    "ContractError",
    "ContractRecord",
    "InitResult",
    "LedgerIntegrityError",
    "PatchError",
    "PatchRecord",
    "SchemaError",
    "SchemaRecord",
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
    "collection",
    "contracts",
    "derive_view",
    "entries",
    "initialize",
    "read_artifact",
    "record_step",
    "schema",
    "set_contracts",
    "set_schema",
    "state",
    "touched_paths",
    "validate",
    "verify",
    "view",
]
