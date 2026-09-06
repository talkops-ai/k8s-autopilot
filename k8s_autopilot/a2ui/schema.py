# Copyright 2025 K8s Autopilot
# A2UI JSON Schema for dynamic UI generation

A2UI_SCHEMA = r'''
{
  "title": "A2UI Message Schema",
  "description": "Describes a JSON payload for an A2UI (Agent to UI) message, which is used to dynamically construct and update user interfaces. A message MUST contain exactly ONE of the action properties: 'beginRendering', 'surfaceUpdate', 'dataModelUpdate', or 'deleteSurface'.",
  "type": "object",
  "properties": {
    "beginRendering": {
      "type": "object",
      "description": "Signals the client to begin rendering a surface with a root component and specific styles.",
      "properties": {
        "surfaceId": {
          "type": "string",
          "description": "The unique identifier for the UI surface to be rendered."
        },
        "root": {
          "type": "string",
          "description": "The ID of the root component to render."
        },
        "styles": {
          "type": "object",
          "description": "Styling information for the UI.",
          "properties": {
            "font": {
              "type": "string",
              "description": "The primary font for the UI."
            },
            "primaryColor": {
              "type": "string",
              "description": "The primary UI color as a hexadecimal code (e.g., '#00BFFF').",
              "pattern": "^#[0-9a-fA-F]{6}$"
            }
          }
        }
      },
      "required": ["root", "surfaceId"]
    },
    "surfaceUpdate": {
      "type": "object",
      "description": "Updates a surface with a new set of components.",
      "properties": {
        "surfaceId": {
          "type": "string",
          "description": "The unique identifier for the UI surface to be updated."
        },
        "components": {
          "type": "array",
          "description": "A list containing all UI components for the surface.",
          "minItems": 1,
          "items": {
            "type": "object",
            "description": "Represents a single component in a UI widget tree.",
            "properties": {
              "id": {
                "type": "string",
                "description": "The unique identifier for this component."
              },
              "weight": {
                "type": "number",
                "description": "The relative weight of this component within a Row or Column (flex-grow)."
              },
              "component": {
                "type": "object",
                "description": "A wrapper object that MUST contain exactly one key, which is the name of the component type.",
                "properties": {
                  "Text": {
                    "type": "object",
                    "properties": {
                      "text": {
                        "type": "object",
                        "description": "The text content to display. Can be literalString or path to data model.",
                        "properties": {
                          "literalString": { "type": "string" },
                          "path": { "type": "string" }
                        }
                      },
                      "usageHint": {
                        "type": "string",
                        "description": "Text style hint: h1, h2, h3, h4, h5, caption, body",
                        "enum": ["h1", "h2", "h3", "h4", "h5", "caption", "body"]
                      }
                    },
                    "required": ["text"]
                  },
                  "Image": {
                    "type": "object",
                    "properties": {
                      "url": {
                        "type": "object",
                        "properties": {
                          "literalString": { "type": "string" },
                          "path": { "type": "string" }
                        }
                      },
                      "fit": {
                        "type": "string",
                        "enum": ["contain", "cover", "fill", "none", "scale-down"]
                      },
                      "usageHint": {
                        "type": "string",
                        "enum": ["icon", "avatar", "smallFeature", "mediumFeature", "largeFeature", "header"]
                      }
                    },
                    "required": ["url"]
                  },
                  "Icon": {
                    "type": "object",
                    "properties": {
                      "name": {
                        "type": "object",
                        "properties": {
                          "literalString": {
                            "type": "string",
                            "enum": ["check", "close", "error", "warning", "info", "help", "settings", "refresh", "download", "upload", "delete", "edit", "add", "search", "folder", "home", "star", "favorite", "send", "share"]
                          },
                          "path": { "type": "string" }
                        }
                      }
                    },
                    "required": ["name"]
                  },
                  "Row": {
                    "type": "object",
                    "properties": {
                      "children": {
                        "type": "object",
                        "properties": {
                          "explicitList": {
                            "type": "array",
                            "items": { "type": "string" }
                          },
                          "template": {
                            "type": "object",
                            "properties": {
                              "componentId": { "type": "string" },
                              "dataBinding": { "type": "string" }
                            },
                            "required": ["componentId", "dataBinding"]
                          }
                        }
                      },
                      "distribution": {
                        "type": "string",
                        "enum": ["center", "end", "spaceAround", "spaceBetween", "spaceEvenly", "start"]
                      },
                      "alignment": {
                        "type": "string",
                        "enum": ["start", "center", "end", "stretch"]
                      }
                    },
                    "required": ["children"]
                  },
                  "Column": {
                    "type": "object",
                    "properties": {
                      "children": {
                        "type": "object",
                        "properties": {
                          "explicitList": {
                            "type": "array",
                            "items": { "type": "string" }
                          },
                          "template": {
                            "type": "object",
                            "properties": {
                              "componentId": { "type": "string" },
                              "dataBinding": { "type": "string" }
                            },
                            "required": ["componentId", "dataBinding"]
                          }
                        }
                      },
                      "distribution": {
                        "type": "string",
                        "enum": ["start", "center", "end", "spaceBetween", "spaceAround", "spaceEvenly"]
                      },
                      "alignment": {
                        "type": "string",
                        "enum": ["center", "end", "start", "stretch"]
                      }
                    },
                    "required": ["children"]
                  },
                  "List": {
                    "type": "object",
                    "properties": {
                      "children": {
                        "type": "object",
                        "properties": {
                          "explicitList": {
                            "type": "array",
                            "items": { "type": "string" }
                          },
                          "template": {
                            "type": "object",
                            "properties": {
                              "componentId": { "type": "string" },
                              "dataBinding": { "type": "string" }
                            },
                            "required": ["componentId", "dataBinding"]
                          }
                        }
                      },
                      "direction": {
                        "type": "string",
                        "enum": ["vertical", "horizontal"]
                      },
                      "alignment": {
                        "type": "string",
                        "enum": ["start", "center", "end", "stretch"]
                      }
                    },
                    "required": ["children"]
                  },
                  "Card": {
                    "type": "object",
                    "properties": {
                      "child": {
                        "type": "string",
                        "description": "The ID of the component to be rendered inside the card."
                      }
                    },
                    "required": ["child"]
                  },
                  "Divider": {
                    "type": "object",
                    "properties": {
                      "axis": {
                        "type": "string",
                        "enum": ["horizontal", "vertical"]
                      }
                    }
                  },
                  "Button": {
                    "type": "object",
                    "properties": {
                      "child": {
                        "type": "string",
                        "description": "The ID of the component to display in the button."
                      },
                      "primary": {
                        "type": "boolean",
                        "description": "Indicates if this button should be styled as the primary action."
                      },
                      "action": {
                        "type": "object",
                        "description": "The action to be dispatched when the button is clicked.",
                        "properties": {
                          "name": { "type": "string" },
                          "context": {
                            "type": "array",
                            "items": {
                              "type": "object",
                              "properties": {
                                "key": { "type": "string" },
                                "value": {
                                  "type": "object",
                                  "properties": {
                                    "path": { "type": "string" },
                                    "literalString": { "type": "string" },
                                    "literalNumber": { "type": "number" },
                                    "literalBoolean": { "type": "boolean" }
                                  }
                                }
                              },
                              "required": ["key", "value"]
                            }
                          }
                        },
                        "required": ["name"]
                      }
                    },
                    "required": ["child", "action"]
                  },
                  "TextField": {
                    "type": "object",
                    "properties": {
                      "label": {
                        "type": "object",
                        "properties": {
                          "literalString": { "type": "string" },
                          "path": { "type": "string" }
                        }
                      },
                      "text": {
                        "type": "object",
                        "properties": {
                          "literalString": { "type": "string" },
                          "path": { "type": "string" }
                        }
                      },
                      "textFieldType": {
                        "type": "string",
                        "enum": ["date", "longText", "number", "shortText", "obscured"]
                      }
                    },
                    "required": ["label"]
                  },
                  "CheckBox": {
                    "type": "object",
                    "properties": {
                      "label": {
                        "type": "object",
                        "properties": {
                          "literalString": { "type": "string" },
                          "path": { "type": "string" }
                        }
                      },
                      "value": {
                        "type": "object",
                        "properties": {
                          "literalBoolean": { "type": "boolean" },
                          "path": { "type": "string" }
                        }
                      }
                    },
                    "required": ["label", "value"]
                  }
                }
              }
            },
            "required": ["id", "component"]
          }
        }
      },
      "required": ["surfaceId", "components"]
    },
    "dataModelUpdate": {
      "type": "object",
      "description": "Updates the data model for a surface.",
      "properties": {
        "surfaceId": {
          "type": "string",
          "description": "The unique identifier for the UI surface this data model update applies to."
        },
        "path": {
          "type": "string",
          "description": "Path to a location within the data model. If '/', the entire data model will be replaced."
        },
        "contents": {
          "type": "array",
          "description": "An array of data entries with key and value.",
          "items": {
            "type": "object",
            "properties": {
              "key": { "type": "string" },
              "valueString": { "type": "string" },
              "valueNumber": { "type": "number" },
              "valueBoolean": { "type": "boolean" },
              "valueMap": {
                "type": "array",
                "items": {
                  "type": "object",
                  "properties": {
                    "key": { "type": "string" },
                    "valueString": { "type": "string" },
                    "valueNumber": { "type": "number" },
                    "valueBoolean": { "type": "boolean" }
                  },
                  "required": ["key"]
                }
              }
            },
            "required": ["key"]
          }
        }
      },
      "required": ["contents", "surfaceId"]
    },
    "deleteSurface": {
      "type": "object",
      "description": "Signals the client to delete the surface identified by 'surfaceId'.",
      "properties": {
        "surfaceId": {
          "type": "string",
          "description": "The unique identifier for the UI surface to be deleted."
        }
      },
      "required": ["surfaceId"]
    }
  }
}
'''
