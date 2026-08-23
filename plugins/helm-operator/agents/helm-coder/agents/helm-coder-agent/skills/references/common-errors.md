# Helm Common Errors

When `helm lint` or `helm template` fail, analyze these common gotchas. You must provide clear instructions so the generator/updater agent knows exactly what went wrong.

## 1. `error calling include: template: ... : no template "X" associated with template "Y"`
- **Cause**: The `_helpers.tpl` file is missing a `define` block for the referenced include, or the generator used `.Chart.Name` instead of `include "my-chart.fullname"` somewhere incorrectly.
- **Fix to relay**: "Tell the generator to verify `_helpers.tpl` has matching `define` mappings for the failed macro."

## 2. `error calling nindent: runtime error: invalid memory address or nil pointer dereference`
- **Cause**: Passing a nil/empty struct into a yaml conversion block `{{- toYaml .Values.missing | nindent 4 }}` when `.Values.missing` does not exist in `values.yaml`.
- **Fix to relay**: "Wrap the toYaml logic in `{{- if .Values.X }}` or `{{- with .Values.X }}` blocks to gracefully handle nil configurations!"

## 3. `function "trimSuffix" not defined`
- **Cause**: Often syntax fat-fingering `|-` or using incorrect go template pipelines.
- **Fix to relay**: "Verify the text casing and pipelining `| trunc 63 | trimSuffix "-"`."

## 4. `unknown field "XYZ" in io.k8s.api.apps.v1.Deployment`
- **Cause**: YAML indentation rules inside the templates failed (often missing an `nindent`).
- **Fix to relay**: "Check the indentation levels generated around XYZ. It is being mapped to the wrong spec layer."
