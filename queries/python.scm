; Function / class definitions
(function_definition
  name: (identifier) @def.function.name) @def.function

(class_definition
  name: (identifier) @def.class.name
  superclasses: (argument_list [
    (identifier) @def.class.base
    (attribute attribute: (identifier) @def.class.base)
  ])?) @def.class

; Call sites
(call
  function: (identifier) @call.name) @call

(call
  function: (attribute attribute: (identifier) @call.name)) @call

; Imports are extracted by a direct tree walk in code_graph.py (field
; names/shape vary too much across import forms for one query pattern),
; not by a query capture here.
