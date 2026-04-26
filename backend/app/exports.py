import csv
import io
import json

from flask import Response


def csv_response(filename, fieldnames, rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


def json_response(filename, payload):
    return Response(
        json.dumps(payload, indent=2, sort_keys=False, default=str),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )
