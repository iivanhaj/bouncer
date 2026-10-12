from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class GatewayError(HTTPException):
    """An error returned to the client in OpenAI's error format, so OpenAI SDKs can parse it.

    Subclasses HTTPException because FastAPI turns any other exception raised while reading the
    request body into a generic 400; the body size limit raises this mid-read and needs its 413.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        type_: str,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(status_code, detail=message, headers=headers)
        self.status_code = status_code
        self.message = message
        self.type = type_
        self.code = code
        self.headers = headers


def error_response(
    status_code: int, message: str, type_: str, code: str | None = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": type_, "code": code}},
        headers=headers,
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def handle_gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        return error_response(exc.status_code, exc.message, exc.type, exc.code, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default is 422 with its own shape; OpenAI clients expect 400 with an "error" object.
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"] if part != "body")
        message = f"{location}: {first['msg']}" if location else first["msg"]
        return error_response(400, message, "invalid_request_error", "invalid_request")
