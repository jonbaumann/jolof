from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class Pagination(PageNumberPagination):
    """?page=&page_size=, answered as {count, page, page_size, total_pages, next, previous, results}."""

    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100

    def get_page_size(self, request):
        # A view can raise the cap, e.g. for the cheap names-only listing.
        self.max_page_size = getattr(request, 'api_max_page_size', type(self).max_page_size)
        self.page_size = getattr(request, 'api_default_page_size', type(self).page_size)
        return super().get_page_size(request)

    def get_paginated_response(self, data):
        return Response({
            'count': self.page.paginator.count,
            'page': self.page.number,
            'page_size': self.page.paginator.per_page,
            'total_pages': self.page.paginator.num_pages,
            'next': self.get_next_link(),
            'previous': self.get_previous_link(),
            'results': data,
        })

    def get_paginated_response_schema(self, schema):
        return {
            'type': 'object',
            'required': ['count', 'page', 'page_size', 'total_pages', 'results'],
            'properties': {
                'count': {'type': 'integer'},
                'page': {'type': 'integer'},
                'page_size': {'type': 'integer'},
                'total_pages': {'type': 'integer'},
                'next': {'type': 'string', 'format': 'uri', 'nullable': True},
                'previous': {'type': 'string', 'format': 'uri', 'nullable': True},
                'results': schema,
            },
        }
