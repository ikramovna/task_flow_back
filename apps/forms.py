from django import forms
from django.utils.html import format_html, format_html_join

from .models import Event


class DatalistInput(forms.TextInput):
    def __init__(self, options=(), attrs=None):
        self.options = options
        super().__init__(attrs)

    def render(self, name, value, attrs=None, renderer=None):
        attrs = attrs or {}
        datalist_id = f"{attrs.get('id', f'id_{name}')}_options"
        attrs["list"] = datalist_id
        text_input = super().render(name, value, attrs, renderer)
        options = format_html_join("", '<option value="{}"></option>', ((option,) for option in self.options))
        return format_html('{}<datalist id="{}">{}</datalist>', text_input, datalist_id, options)


class EventAdminForm(forms.ModelForm):
    event_type = forms.CharField(
        max_length=80,
        widget=DatalistInput(
            options=[label for _, label in Event.Type.choices],
            attrs={"placeholder": "Select or type a custom event type"},
        ),
        help_text="Choose a suggested type or enter your own.",
    )

    class Meta:
        model = Event
        fields = "__all__"
