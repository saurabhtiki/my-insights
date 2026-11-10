from django import template

from ..loading import display_value

register = template.Library()


@register.filter
def cell(row: dict, key: str) -> str:
    """A data cell as the user should read it, e.g. {{ row|cell:column.name }}."""
    return display_value(row.get(key))
