from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.auth.router import router as auth_router
from app.dashboard.router import router as dashboard_router
from app.documents.router import router as documents_router
from app.facts.router import router as facts_router
from app.jobs.router import router as jobs_router
from app.models.router import router as models_router
from app.prompts.router import router as prompts_router
from app.profiles.router import router as profiles_router
from app.projects.router import router as projects_router
from app.reviews.router import router as reviews_router
from app.exports.router import router as exports_router


_SAFE_VALIDATION_MESSAGES = {
    "bool_parsing": "invalid boolean",
    "dict_type": "invalid object",
    "extra_forbidden": "extra field not permitted",
    "finite_number": "finite number required",
    "greater_than_equal": "value out of range",
    "int_parsing": "invalid integer",
    "less_than_equal": "value out of range",
    "list_type": "invalid list",
    "literal_error": "invalid choice",
    "missing": "field required",
    "string_too_long": "string too long",
    "string_too_short": "string too short",
    "tuple_type": "invalid list",
    "uuid_parsing": "invalid UUID",
    "uuid_type": "invalid UUID",
    "value_error": "invalid value",
}


def _safe_validation_message(error_type: str) -> str:
    return _SAFE_VALIDATION_MESSAGES.get(error_type, "invalid value")


def _sanitized_error_location(location: tuple[object, ...]) -> tuple[object, ...]:
    sensitive_markers = (
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "credential",
        "header",
        "password",
        "path",
        "secret",
        "token",
    )
    safe: list[object] = []
    for segment in location:
        if isinstance(segment, str):
            lowered = segment.lower()
            if (
                any(marker in lowered for marker in sensitive_markers)
                or "/" in segment
                or "\\" in segment
                or not segment.isprintable()
            ):
                safe.append("<redacted>")
                continue
        safe.append(segment)
    return tuple(safe)


def create_app() -> FastAPI:
    app = FastAPI(title="Extraction Review Platform")

    @app.exception_handler(RequestValidationError)
    async def sanitized_validation_error(
        _request: object, _exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic errors include the rejected input and exception context by
        # default. Keep the machine-readable contract without reflecting either.
        detail = [
            {
                "loc": _sanitized_error_location(error.get("loc", ())),
                "type": error.get("type", "value_error"),
                "msg": _safe_validation_message(
                    error.get("type", "value_error")
                ),
            }
            for error in _exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(projects_router)
    app.include_router(documents_router)
    app.include_router(facts_router)
    app.include_router(profiles_router)
    app.include_router(jobs_router)
    app.include_router(models_router)
    app.include_router(prompts_router)
    app.include_router(reviews_router)
    app.include_router(exports_router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "extraction-review-platform"}

    return app


app = create_app()
