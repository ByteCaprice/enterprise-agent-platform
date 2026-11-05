"""Bounded scoped string-key cursors. Every page must rebuild its authorization query."""

import base64
import hashlib
import json

from sqlalchemy import tuple_


def string_key_page(session, query, columns, *, scope, limit=50, cursor=None, serialize):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Page limit must be between 1 and 200")
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    if cursor is not None:
        try:
            if not 1 <= len(cursor) <= 4096:
                raise ValueError
            payload = json.loads(base64.b64decode(cursor.encode(), altchars=b"-_", validate=True))
            values = payload["keys"]
            if payload["scope"] != digest or len(values) != len(columns) or any(
                    not isinstance(value, str) or len(value) > 500 or "\x00" in value for value in values):
                raise ValueError
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ValueError("Invalid page cursor") from None
        query = query.where(tuple_(*columns) > tuple(values))
    rows = session.scalars(query.order_by(*columns).limit(limit + 1)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more:
        keys = [getattr(rows[-1], column.key) for column in columns]
        next_cursor = base64.urlsafe_b64encode(json.dumps({"scope": digest, "keys": keys}).encode()).decode()
    return {"items": [serialize(row) for row in rows], "next_cursor": next_cursor, "limit": limit}
