"""DRF 异常处理：把更正服务的领域异常翻译成 HTTP 响应。

* CorrectionValidationError → 400
* CorrectionConflict / CorrectionStateError → 409
"""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .corrections import (
    CorrectionConflict, CorrectionStateError, CorrectionValidationError,
)


def custom_exception_handler(exc, context):
    if isinstance(exc, CorrectionConflict):
        return Response(
            {"detail": exc.detail, "code": exc.code},
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, CorrectionStateError):
        return Response(
            {"detail": exc.detail, "code": "invalid_state"},
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, CorrectionValidationError):
        return Response(
            {"detail": exc.detail, "code": "invalid_correction"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return drf_exception_handler(exc, context)
