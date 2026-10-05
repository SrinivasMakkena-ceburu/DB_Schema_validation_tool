from django.http import HttpResponse
from django.shortcuts import get_object_or_404

from .models import DataOperation


def op_detail(request, pk):
    op = get_object_or_404(DataOperation, pk=pk)
    return HttpResponse(str(op))
