"""DRF 异常处理：把领域层 CorrectionConflict 映射为 HTTP 409。"""
from rest_framework.views import exception_handler

from .models import CorrectionConflict


def serialreg_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if isinstance(exc, CorrectionConflict):
        from rest_framework.response import Response
        return Response(
            {
                "detail": exc.detail,
                "code": exc.code,
                "conflicts": exc.conflicts,
            },
            status=409,
        )
    return response
