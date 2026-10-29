"""Writing recipes out as CSV.

The columns themselves live in csv_fields, shared with the import so the two
tools agree on the format.
"""

import csv

from django.http import HttpResponse

from . import csv_fields


def build_csv_response(recipes, keys, filename):
    fields = csv_fields.ordered_fields(keys)

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    # Byte-order mark so Excel reads the file as UTF-8 rather than mangling
    # any accented ingredient names. The import decodes as utf-8-sig, so this
    # doesn't get in the way of reading the file back in.
    response.write('\ufeff')

    writer = csv.writer(response)
    writer.writerow([field.key for field in fields])
    for recipe in recipes:
        writer.writerow([field.extract(recipe) for field in fields])

    return response
