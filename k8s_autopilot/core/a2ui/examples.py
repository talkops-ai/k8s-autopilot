# Copyright 2025 K8s Autopilot
# K8s/Helm-specific A2UI examples for LLM few-shot learning

K8S_UI_EXAMPLES = """
---BEGIN WORKFLOW_STATUS_EXAMPLE---
[
  {{ "beginRendering": {{ "surfaceId": "workflow-status", "root": "status-root", "styles": {{ "primaryColor": "#326CE5", "font": "Roboto Mono" }} }} }},
  {{ "surfaceUpdate": {{
    "surfaceId": "workflow-status",
    "components": [
      {{ "id": "status-root", "component": {{ "Column": {{ "children": {{ "explicitList": ["workflow-header", "phase-list", "current-action"] }} }} }} }},
      {{ "id": "workflow-header", "component": {{ "Row": {{ "children": {{ "explicitList": ["k8s-icon", "workflow-title"] }}, "alignment": "center" }} }} }},
      {{ "id": "k8s-icon", "component": {{ "Icon": {{ "name": {{ "literalString": "settings" }} }} }} }},
      {{ "id": "workflow-title", "component": {{ "Text": {{ "usageHint": "h2", "text": {{ "path": "workflowTitle" }} }} }} }},
      {{ "id": "phase-list", "component": {{ "List": {{ "direction": "vertical", "children": {{ "template": {{ "componentId": "phase-item-template", "dataBinding": "/phases" }} }} }} }} }},
      {{ "id": "phase-item-template", "component": {{ "Card": {{ "child": "phase-row" }} }} }},
      {{ "id": "phase-row", "component": {{ "Row": {{ "children": {{ "explicitList": ["phase-icon", "phase-name", "phase-status"] }}, "distribution": "spaceBetween", "alignment": "center" }} }} }},
      {{ "id": "phase-icon", "component": {{ "Icon": {{ "name": {{ "path": "icon" }} }} }} }},
      {{ "id": "phase-name", "component": {{ "Text": {{ "usageHint": "body", "text": {{ "path": "name" }} }} }} }},
      {{ "id": "phase-status", "component": {{ "Text": {{ "usageHint": "caption", "text": {{ "path": "status" }} }} }} }},
      {{ "id": "current-action", "component": {{ "Text": {{ "usageHint": "caption", "text": {{ "path": "currentAction" }} }} }} }}
    ]
  }} }},
  {{ "dataModelUpdate": {{
    "surfaceId": "workflow-status",
    "path": "/",
    "contents": [
      {{ "key": "workflowTitle", "valueString": "Helm Chart Generation Workflow" }},
      {{ "key": "phases", "valueMap": [
        {{ "key": "planning", "valueMap": [
          {{ "key": "name", "valueString": "📋 Planning" }},
          {{ "key": "status", "valueString": "✅ Complete" }},
          {{ "key": "icon", "valueString": "check" }}
        ] }},
        {{ "key": "generation", "valueMap": [
          {{ "key": "name", "valueString": "🔧 Generation" }},
          {{ "key": "status", "valueString": "🔄 In Progress" }},
          {{ "key": "icon", "valueString": "settings" }}
        ] }},
        {{ "key": "validation", "valueMap": [
          {{ "key": "name", "valueString": "🔍 Validation" }},
          {{ "key": "status", "valueString": "⏳ Pending" }},
          {{ "key": "icon", "valueString": "info" }}
        ] }}
      ] }},
      {{ "key": "currentAction", "valueString": "Generating deployment.yaml template..." }}
    ]
  }} }}
]
---END WORKFLOW_STATUS_EXAMPLE---

---BEGIN HITL_APPROVAL_FORM_EXAMPLE---
[
  {{ "beginRendering": {{ "surfaceId": "approval-form", "root": "approval-root", "styles": {{ "primaryColor": "#326CE5", "font": "Roboto" }} }} }},
  {{ "surfaceUpdate": {{
    "surfaceId": "approval-form",
    "components": [
      {{ "id": "approval-root", "component": {{ "Card": {{ "child": "approval-content" }} }} }},
      {{ "id": "approval-content", "component": {{ "Column": {{ "children": {{ "explicitList": ["approval-header", "divider1", "approval-details", "divider2", "approval-actions"] }} }} }} }},
      {{ "id": "approval-header", "component": {{ "Row": {{ "children": {{ "explicitList": ["warning-icon", "approval-title"] }}, "alignment": "center" }} }} }},
      {{ "id": "warning-icon", "component": {{ "Icon": {{ "name": {{ "literalString": "warning" }} }} }} }},
      {{ "id": "approval-title", "component": {{ "Text": {{ "usageHint": "h3", "text": {{ "path": "title" }} }} }} }},
      {{ "id": "divider1", "component": {{ "Divider": {{}} }} }},
      {{ "id": "approval-details", "component": {{ "Column": {{ "children": {{ "explicitList": ["phase-text", "question-text", "context-text"] }} }} }} }},
      {{ "id": "phase-text", "component": {{ "Text": {{ "usageHint": "caption", "text": {{ "path": "phase" }} }} }} }},
      {{ "id": "question-text", "component": {{ "Text": {{ "usageHint": "body", "text": {{ "path": "question" }} }} }} }},
      {{ "id": "context-text", "component": {{ "Text": {{ "usageHint": "caption", "text": {{ "path": "context" }} }} }} }},
      {{ "id": "divider2", "component": {{ "Divider": {{}} }} }},
      {{ "id": "approval-actions", "component": {{ "Row": {{ "children": {{ "explicitList": ["reject-button", "approve-button"] }}, "distribution": "spaceEvenly" }} }} }},
      {{ "id": "reject-button", "component": {{ "Button": {{ "child": "reject-text", "primary": false, "action": {{ "name": "hitl_response", "context": [{{ "key": "decision", "value": {{ "literalString": "rejected" }} }}, {{ "key": "phase", "value": {{ "path": "phaseId" }} }}] }} }} }} }},
      {{ "id": "reject-text", "component": {{ "Text": {{ "text": {{ "literalString": "❌ Reject" }} }} }} }},
      {{ "id": "approve-button", "component": {{ "Button": {{ "child": "approve-text", "primary": true, "action": {{ "name": "hitl_response", "context": [{{ "key": "decision", "value": {{ "literalString": "approved" }} }}, {{ "key": "phase", "value": {{ "path": "phaseId" }} }}] }} }} }} }},
      {{ "id": "approve-text", "component": {{ "Text": {{ "text": {{ "literalString": "✅ Approve" }} }} }} }}
    ]
  }} }},
  {{ "dataModelUpdate": {{
    "surfaceId": "approval-form",
    "path": "/",
    "contents": [
      {{ "key": "title", "valueString": "Human Review Required" }},
      {{ "key": "phase", "valueString": "Phase: Generation Review" }},
      {{ "key": "phaseId", "valueString": "generation" }},
      {{ "key": "question", "valueString": "Please review the generated Helm chart templates. Do you want to proceed with validation?" }},
      {{ "key": "context", "valueString": "Generated 5 template files in /tmp/helm-charts/my-app" }}
    ]
  }} }}
]
---END HITL_APPROVAL_FORM_EXAMPLE---

---BEGIN HELM_CHART_LIST_EXAMPLE---
[
  {{ "beginRendering": {{ "surfaceId": "chart-files", "root": "files-root", "styles": {{ "primaryColor": "#326CE5", "font": "Roboto Mono" }} }} }},
  {{ "surfaceUpdate": {{
    "surfaceId": "chart-files",
    "components": [
      {{ "id": "files-root", "component": {{ "Column": {{ "children": {{ "explicitList": ["files-header", "files-list", "actions-row"] }} }} }} }},
      {{ "id": "files-header", "component": {{ "Row": {{ "children": {{ "explicitList": ["folder-icon", "chart-name"] }}, "alignment": "center" }} }} }},
      {{ "id": "folder-icon", "component": {{ "Icon": {{ "name": {{ "literalString": "folder" }} }} }} }},
      {{ "id": "chart-name", "component": {{ "Text": {{ "usageHint": "h2", "text": {{ "path": "chartName" }} }} }} }},
      {{ "id": "files-list", "component": {{ "List": {{ "direction": "vertical", "children": {{ "template": {{ "componentId": "file-item-template", "dataBinding": "/files" }} }} }} }} }},
      {{ "id": "file-item-template", "component": {{ "Card": {{ "child": "file-row" }} }} }},
      {{ "id": "file-row", "component": {{ "Row": {{ "children": {{ "explicitList": ["file-icon", "file-info"] }}, "alignment": "center" }} }} }},
      {{ "id": "file-icon", "component": {{ "Icon": {{ "name": {{ "literalString": "edit" }} }} }} }},
      {{ "id": "file-info", "component": {{ "Column": {{ "children": {{ "explicitList": ["file-path", "file-size"] }} }} }} }},
      {{ "id": "file-path", "component": {{ "Text": {{ "usageHint": "body", "text": {{ "path": "path" }} }} }} }},
      {{ "id": "file-size", "component": {{ "Text": {{ "usageHint": "caption", "text": {{ "path": "size" }} }} }} }},
      {{ "id": "actions-row", "component": {{ "Row": {{ "children": {{ "explicitList": ["download-btn", "deploy-btn"] }}, "distribution": "spaceEvenly" }} }} }},
      {{ "id": "download-btn", "component": {{ "Button": {{ "child": "download-text", "primary": false, "action": {{ "name": "download_chart", "context": [{{ "key": "chartPath", "value": {{ "path": "chartPath" }} }}] }} }} }} }},
      {{ "id": "download-text", "component": {{ "Text": {{ "text": {{ "literalString": "📥 Download" }} }} }} }},
      {{ "id": "deploy-btn", "component": {{ "Button": {{ "child": "deploy-text", "primary": true, "action": {{ "name": "deploy_chart", "context": [{{ "key": "chartPath", "value": {{ "path": "chartPath" }} }}] }} }} }} }},
      {{ "id": "deploy-text", "component": {{ "Text": {{ "text": {{ "literalString": "🚀 Deploy" }} }} }} }}
    ]
  }} }},
  {{ "dataModelUpdate": {{
    "surfaceId": "chart-files",
    "path": "/",
    "contents": [
      {{ "key": "chartName", "valueString": "📦 my-fastapi-app" }},
      {{ "key": "chartPath", "valueString": "/tmp/helm-charts/my-fastapi-app" }},
      {{ "key": "files", "valueMap": [
        {{ "key": "file1", "valueMap": [
          {{ "key": "path", "valueString": "Chart.yaml" }},
          {{ "key": "size", "valueString": "423 bytes" }}
        ] }},
        {{ "key": "file2", "valueMap": [
          {{ "key": "path", "valueString": "values.yaml" }},
          {{ "key": "size", "valueString": "1.2 KB" }}
        ] }},
        {{ "key": "file3", "valueMap": [
          {{ "key": "path", "valueString": "templates/deployment.yaml" }},
          {{ "key": "size", "valueString": "2.1 KB" }}
        ] }},
        {{ "key": "file4", "valueMap": [
          {{ "key": "path", "valueString": "templates/service.yaml" }},
          {{ "key": "size", "valueString": "512 bytes" }}
        ] }}
      ] }}
    ]
  }} }}
]
---END HELM_CHART_LIST_EXAMPLE---

---BEGIN VALIDATION_RESULTS_EXAMPLE---
[
  {{ "beginRendering": {{ "surfaceId": "validation-results", "root": "validation-root", "styles": {{ "primaryColor": "#326CE5", "font": "Roboto Mono" }} }} }},
  {{ "surfaceUpdate": {{
    "surfaceId": "validation-results",
    "components": [
      {{ "id": "validation-root", "component": {{ "Column": {{ "children": {{ "explicitList": ["validation-header", "results-list", "summary-card"] }} }} }} }},
      {{ "id": "validation-header", "component": {{ "Row": {{ "children": {{ "explicitList": ["val-icon", "val-title"] }}, "alignment": "center" }} }} }},
      {{ "id": "val-icon", "component": {{ "Icon": {{ "name": {{ "path": "overallIcon" }} }} }} }},
      {{ "id": "val-title", "component": {{ "Text": {{ "usageHint": "h2", "text": {{ "path": "title" }} }} }} }},
      {{ "id": "results-list", "component": {{ "List": {{ "direction": "vertical", "children": {{ "template": {{ "componentId": "result-item-template", "dataBinding": "/checks" }} }} }} }} }},
      {{ "id": "result-item-template", "component": {{ "Card": {{ "child": "result-row" }} }} }},
      {{ "id": "result-row", "component": {{ "Row": {{ "children": {{ "explicitList": ["result-icon", "result-info"] }}, "alignment": "center", "distribution": "start" }} }} }},
      {{ "id": "result-icon", "component": {{ "Icon": {{ "name": {{ "path": "icon" }} }} }} }},
      {{ "id": "result-info", "component": {{ "Column": {{ "children": {{ "explicitList": ["check-name", "check-details"] }} }} }} }},
      {{ "id": "check-name", "component": {{ "Text": {{ "usageHint": "body", "text": {{ "path": "name" }} }} }} }},
      {{ "id": "check-details", "component": {{ "Text": {{ "usageHint": "caption", "text": {{ "path": "details" }} }} }} }},
      {{ "id": "summary-card", "component": {{ "Card": {{ "child": "summary-content" }} }} }},
      {{ "id": "summary-content", "component": {{ "Row": {{ "children": {{ "explicitList": ["passed-count", "failed-count", "warnings-count"] }}, "distribution": "spaceEvenly" }} }} }},
      {{ "id": "passed-count", "component": {{ "Text": {{ "usageHint": "body", "text": {{ "path": "passedText" }} }} }} }},
      {{ "id": "failed-count", "component": {{ "Text": {{ "usageHint": "body", "text": {{ "path": "failedText" }} }} }} }},
      {{ "id": "warnings-count", "component": {{ "Text": {{ "usageHint": "body", "text": {{ "path": "warningsText" }} }} }} }}
    ]
  }} }},
  {{ "dataModelUpdate": {{
    "surfaceId": "validation-results",
    "path": "/",
    "contents": [
      {{ "key": "title", "valueString": "🔍 Validation Results" }},
      {{ "key": "overallIcon", "valueString": "check" }},
      {{ "key": "passedText", "valueString": "✅ Passed: 4" }},
      {{ "key": "failedText", "valueString": "❌ Failed: 0" }},
      {{ "key": "warningsText", "valueString": "⚠️ Warnings: 1" }},
      {{ "key": "checks", "valueMap": [
        {{ "key": "lint", "valueMap": [
          {{ "key": "name", "valueString": "Helm Lint" }},
          {{ "key": "details", "valueString": "Chart linted successfully - no issues found" }},
          {{ "key": "icon", "valueString": "check" }}
        ] }},
        {{ "key": "template", "valueMap": [
          {{ "key": "name", "valueString": "Template Render" }},
          {{ "key": "details", "valueString": "All templates rendered without errors" }},
          {{ "key": "icon", "valueString": "check" }}
        ] }},
        {{ "key": "security", "valueMap": [
          {{ "key": "name", "valueString": "Security Scan" }},
          {{ "key": "details", "valueString": "No critical vulnerabilities detected" }},
          {{ "key": "icon", "valueString": "check" }}
        ] }},
        {{ "key": "best-practices", "valueMap": [
          {{ "key": "name", "valueString": "Best Practices" }},
          {{ "key": "details", "valueString": "Warning: Consider adding resource limits" }},
          {{ "key": "icon", "valueString": "warning" }}
        ] }}
      ] }}
    ]
  }} }}
]
---END VALIDATION_RESULTS_EXAMPLE---

---BEGIN HELM_RELEASE_TABLE_EXAMPLE---
[
  {{ "beginRendering": {{ "surfaceId": "releases-table", "root": "releases-root", "styles": {{ "primaryColor": "#326CE5", "font": "Roboto Mono" }} }} }},
  {{ "surfaceUpdate": {{
    "surfaceId": "releases-table",
    "components": [
      {{ "id": "releases-root", "component": {{ "Column": {{ "children": {{ "explicitList": ["releases-header", "releases-list"] }} }} }} }},
      {{ "id": "releases-header", "component": {{ "Row": {{ "children": {{ "explicitList": ["helm-icon", "releases-title"] }}, "alignment": "center" }} }} }},
      {{ "id": "helm-icon", "component": {{ "Icon": {{ "name": {{ "literalString": "settings" }} }} }} }},
      {{ "id": "releases-title", "component": {{ "Text": {{ "usageHint": "h2", "text": {{ "path": "title" }} }} }} }},
      {{ "id": "releases-list", "component": {{ "List": {{ "direction": "vertical", "children": {{ "template": {{ "componentId": "release-row-template", "dataBinding": "/releases" }} }} }} }} }},
      {{ "id": "release-row-template", "component": {{ "Card": {{ "child": "release-card-content" }} }} }},
      {{ "id": "release-card-content", "component": {{ "Row": {{ "children": {{ "explicitList": ["release-info", "release-status", "release-actions"] }}, "distribution": "spaceBetween", "alignment": "center" }} }} }},
      {{ "id": "release-info", "component": {{ "Column": {{ "children": {{ "explicitList": ["release-name", "release-namespace"] }} }} }} }},
      {{ "id": "release-name", "component": {{ "Text": {{ "usageHint": "h4", "text": {{ "path": "name" }} }} }} }},
      {{ "id": "release-namespace", "component": {{ "Text": {{ "usageHint": "caption", "text": {{ "path": "namespace" }} }} }} }},
      {{ "id": "release-status", "component": {{ "Column": {{ "children": {{ "explicitList": ["status-badge", "revision-text"] }} }} }} }},
      {{ "id": "status-badge", "component": {{ "Text": {{ "usageHint": "body", "text": {{ "path": "status" }} }} }} }},
      {{ "id": "revision-text", "component": {{ "Text": {{ "usageHint": "caption", "text": {{ "path": "revision" }} }} }} }},
      {{ "id": "release-actions", "component": {{ "Row": {{ "children": {{ "explicitList": ["upgrade-btn", "uninstall-btn"] }} }} }} }},
      {{ "id": "upgrade-btn", "component": {{ "Button": {{ "child": "upgrade-text", "primary": true, "action": {{ "name": "upgrade_release", "context": [{{ "key": "releaseName", "value": {{ "path": "name" }} }}, {{ "key": "namespace", "value": {{ "path": "namespace" }} }}] }} }} }} }},
      {{ "id": "upgrade-text", "component": {{ "Text": {{ "text": {{ "literalString": "⬆️" }} }} }} }},
      {{ "id": "uninstall-btn", "component": {{ "Button": {{ "child": "uninstall-text", "primary": false, "action": {{ "name": "uninstall_release", "context": [{{ "key": "releaseName", "value": {{ "path": "name" }} }}, {{ "key": "namespace", "value": {{ "path": "namespace" }} }}] }} }} }} }},
      {{ "id": "uninstall-text", "component": {{ "Text": {{ "text": {{ "literalString": "🗑️" }} }} }} }}
    ]
  }} }},
  {{ "dataModelUpdate": {{
    "surfaceId": "releases-table",
    "path": "/",
    "contents": [
      {{ "key": "title", "valueString": "⎈ Helm Releases" }},
      {{ "key": "releases", "valueMap": [
        {{ "key": "release1", "valueMap": [
          {{ "key": "name", "valueString": "nginx-ingress" }},
          {{ "key": "namespace", "valueString": "ingress-nginx" }},
          {{ "key": "status", "valueString": "✅ deployed" }},
          {{ "key": "revision", "valueString": "Rev: 3" }}
        ] }},
        {{ "key": "release2", "valueMap": [
          {{ "key": "name", "valueString": "prometheus" }},
          {{ "key": "namespace", "valueString": "monitoring" }},
          {{ "key": "status", "valueString": "✅ deployed" }},
          {{ "key": "revision", "valueString": "Rev: 5" }}
        ] }},
        {{ "key": "release3", "valueMap": [
          {{ "key": "name", "valueString": "my-app" }},
          {{ "key": "namespace", "valueString": "default" }},
          {{ "key": "status", "valueString": "⚠️ pending-upgrade" }},
          {{ "key": "revision", "valueString": "Rev: 1" }}
        ] }}
      ] }}
    ]
  }} }}
]
---END HELM_RELEASE_TABLE_EXAMPLE---

---BEGIN COMPLETION_EXAMPLE---
[
  {{ "beginRendering": {{ "surfaceId": "completion", "root": "completion-card", "styles": {{ "primaryColor": "#22C55E", "font": "Roboto" }} }} }},
  {{ "surfaceUpdate": {{
    "surfaceId": "completion",
    "components": [
      {{ "id": "completion-card", "component": {{ "Card": {{ "child": "completion-content" }} }} }},
      {{ "id": "completion-content", "component": {{ "Column": {{ "children": {{ "explicitList": ["success-header", "divider", "completion-details", "next-steps"] }} }} }} }},
      {{ "id": "success-header", "component": {{ "Row": {{ "children": {{ "explicitList": ["check-icon", "success-title"] }}, "alignment": "center" }} }} }},
      {{ "id": "check-icon", "component": {{ "Icon": {{ "name": {{ "literalString": "check" }} }} }} }},
      {{ "id": "success-title", "component": {{ "Text": {{ "usageHint": "h2", "text": {{ "path": "title" }} }} }} }},
      {{ "id": "divider", "component": {{ "Divider": {{}} }} }},
      {{ "id": "completion-details", "component": {{ "Text": {{ "usageHint": "body", "text": {{ "path": "details" }} }} }} }},
      {{ "id": "next-steps", "component": {{ "Text": {{ "usageHint": "caption", "text": {{ "path": "nextSteps" }} }} }} }}
    ]
  }} }},
  {{ "dataModelUpdate": {{
    "surfaceId": "completion",
    "path": "/",
    "contents": [
      {{ "key": "title", "valueString": "✅ Workflow Complete" }},
      {{ "key": "details", "valueString": "Your Helm chart has been generated, validated, and is ready for deployment." }},
      {{ "key": "nextSteps", "valueString": "Next: Run 'helm install my-app ./my-app' to deploy to your cluster" }}
    ]
  }} }}
]
---END COMPLETION_EXAMPLE---
"""
