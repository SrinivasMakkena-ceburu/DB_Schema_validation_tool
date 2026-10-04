from pathlib import Path

from django import forms

from .models import DatabaseTarget, Project


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name", "path", "python_path", "settings_module", "extra_env"]
        labels = {"python_path": "Python interpreter", "extra_env": "Extra environment variables"}
        widgets = {"extra_env": forms.Textarea(attrs={"rows": 4, "placeholder": "SECRET_KEY=dummy\nDEBUG=0"})}

    def clean_path(self):
        path = self.cleaned_data["path"].strip()
        if not (Path(path) / "manage.py").is_file():
            raise forms.ValidationError("No manage.py in this folder.")
        return path

    def clean_python_path(self):
        python = self.cleaned_data["python_path"].strip()
        if not Path(python).is_file():
            raise forms.ValidationError("No file at this path.")
        return python


class DatabaseForm(forms.ModelForm):
    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="Stored encrypted. Leave blank to keep the saved password, or to use ~/.pgpass.",
    )
    clear_password = forms.BooleanField(required=False, label="Remove saved password")

    class Meta:
        model = DatabaseTarget
        fields = ["name", "host", "port", "dbname", "user", "password", "clear_password", "sslmode", "schema", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def save(self, commit=True):
        database = super().save(commit=False)
        if self.cleaned_data.get("password"):
            database.set_password(self.cleaned_data["password"])
        elif self.cleaned_data.get("clear_password"):
            database.set_password("")
        if commit:
            database.save()
        return database


class ComparisonForm(forms.Form):
    project = forms.ModelChoiceField(queryset=Project.objects.all(), empty_label=None)
    databases = forms.ModelMultipleChoiceField(
        queryset=DatabaseTarget.objects.all(), widget=forms.CheckboxSelectMultiple
    )
    history_strategy = forms.ChoiceField(
        choices=[
            ("fake", "Fake-apply the branch's migrations (the schema SQL brings the database to the branch)"),
            ("leave", "Leave unapplied migrations for manage.py migrate"),
        ],
        initial="fake",
        widget=forms.RadioSelect,
    )
    include_other_apps = forms.BooleanField(
        required=False, label="Also delete history rows of apps that are not in this branch"
    )

    def options(self):
        return {
            "history_strategy": self.cleaned_data["history_strategy"],
            "include_other_apps": self.cleaned_data["include_other_apps"],
        }
