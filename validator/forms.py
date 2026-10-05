from pathlib import Path

from django import forms

from .models import CleanupRecipe, DatabaseTarget, Project


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
    write_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="Password of the write user. Leave blank to keep the saved one.",
    )
    clear_write_password = forms.BooleanField(required=False, label="Remove saved write password")

    class Meta:
        model = DatabaseTarget
        fields = ["name", "environment", "host", "port", "dbname", "user", "password", "clear_password",
                  "sslmode", "schema", "notes", "writes_enabled", "write_user", "write_password",
                  "clear_write_password", "operation_timeout_s"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def save(self, commit=True):
        database = super().save(commit=False)
        if self.cleaned_data.get("password"):
            database.set_password(self.cleaned_data["password"])
        elif self.cleaned_data.get("clear_password"):
            database.set_password("")
        if self.cleaned_data.get("write_password"):
            database.set_write_password(self.cleaned_data["write_password"])
        elif self.cleaned_data.get("clear_write_password"):
            database.set_write_password("")
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


class RecipeForm(forms.ModelForm):
    class Meta:
        model = CleanupRecipe
        fields = ["name", "project", "root_table", "batch_size", "notes"]
        labels = {"root_table": "Table", "project": "Project (cascade rules)"}
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        help_texts = {"batch_size": "Rows of the table deleted per transaction (with their cascades)"}

    def clean_batch_size(self):
        size = self.cleaned_data["batch_size"]
        if not 1 <= size <= 100_000:
            raise forms.ValidationError("Use a batch size between 1 and 100000.")
        return size
