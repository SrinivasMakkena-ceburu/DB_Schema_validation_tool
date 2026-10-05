from django import template

from ..schema_diff import finding_object as _finding_object

register = template.Library()

# category: (label, diff marker). "+" the branch needs it in the database,
# "−" the database has it and the branch does not, "~" it differs,
# "!" the history is broken, "?" no migration file.
CATEGORIES = {
    "table_missing": ("Table missing", "+"),
    "column_missing": ("Column missing", "+"),
    "type_mismatch": ("Type differs", "~"),
    "null_mismatch": ("Nullability differs", "~"),
    "pk_mismatch": ("Primary key differs", "~"),
    "fk_wrong": ("Foreign key target differs", "~"),
    "fk_missing": ("Foreign key missing", "+"),
    "unique_missing": ("Unique constraint missing", "+"),
    "index_missing": ("Index missing", "+"),
    "table_extra": ("Table not in branch", "−"),
    "column_extra": ("Column not in model", "−"),
    "history_missing": ("No django_migrations table", "+"),
    "ghost": ("Applied, file not in branch", "−"),
    "other_app_rows": ("Rows for apps not in branch", "−"),
    "unapplied": ("In branch, not applied", "+"),
    "inconsistent": ("Inconsistent history", "!"),
    "multiple_leaves": ("Needs a merge migration", "!"),
    "pending_change": ("Model change without migration", "?"),
}
CATEGORY_ORDER = list(CATEGORIES)


@register.filter
def category_label(category):
    return CATEGORIES.get(category, (category, ""))[0]


@register.filter
def marker(category):
    return CATEGORIES.get(category, ("", "·"))[1]


@register.filter
def get(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def finding_object(f):
    return _finding_object(f)


MARKER_CLASSES = {"+": "need", "−": "extra", "~": "diff", "!": "broken", "?": "nomig"}


@register.filter
def marker_class(category):
    return MARKER_CLASSES.get(marker(category), "")
