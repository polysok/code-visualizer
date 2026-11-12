# src/code_visualizer/tools/languages/csharp.py
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import re
from code_visualizer.utils.debug_log import debug_log, info_logger, error_logger, warning_logger
from code_visualizer.utils.tree_sitter_manager import execute_query

CSHARP_QUERIES = {
    # Fields and locals share a shape: `variable_declaration` carries the type
    # and holds one `variable_declarator` per name. There was no `variables`
    # query at all and the key was hardcoded to [], so C# produced no Variable
    # nodes — and `resolution/calls.py` lost the local-variable declarations it
    # uses to infer a receiver's type, so `var s = new UserService(); s.Save();`
    # could not resolve Save to UserService.Save.
    "variables": """
        (field_declaration
            (variable_declaration) @decl
        ) @field_node

        (local_declaration_statement
            (variable_declaration) @decl
        ) @local_node
    """,
    "functions": """
        (method_declaration
            name: (identifier) @name
            parameters: (parameter_list) @params
        ) @function_node
        
        (constructor_declaration
            name: (identifier) @name
            parameters: (parameter_list) @params
        ) @function_node
        
        (local_function_statement
            name: (identifier) @name
            parameters: (parameter_list) @params
        ) @function_node
    """,
    "classes": """
        (class_declaration 
            name: (identifier) @name
            (base_list)? @bases
        ) @class
    """,
    "interfaces": """
        (interface_declaration 
            name: (identifier) @name
            (base_list)? @bases
        ) @interface
    """,
    "structs": """
        (struct_declaration 
            name: (identifier) @name
            (base_list)? @bases
        ) @struct
    """,
    "enums": """
        (enum_declaration 
            name: (identifier) @name
        ) @enum
    """,
    "records": """
        (record_declaration 
            name: (identifier) @name
            (base_list)? @bases
        ) @record
    """,
    "properties": """
        (property_declaration
            name: (identifier) @name
        ) @property
    """,
    "imports": """
        (using_directive) @import
    """,
    "calls": """
        (invocation_expression
            function: [
                (identifier) @name
                (member_access_expression
                    name: (identifier) @name
                ) @full_name
            ]
        )
        
        (object_creation_expression
            type: [
                (identifier) @name
                (qualified_name) @name
            ]
        )
    """,

}

class CSharpTreeSitterParser:
    def __init__(self, generic_parser_wrapper: Any):
        self.generic_parser_wrapper = generic_parser_wrapper
        self.language_name = "c_sharp"
        self.language = generic_parser_wrapper.language
        self.parser = generic_parser_wrapper.parser

    def parse(self, path: Path, is_dependency: bool = False, index_source: bool = False) -> Dict[str, Any]:
        try:
            self.index_source = index_source
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                source_code = f.read()

            if not source_code.strip():
                warning_logger(f"Empty or whitespace-only file: {path}")
                return {
                    "path": str(path),
                    "functions": [],
                    "classes": [],
                    "interfaces": [],
                    "structs": [],
                    "enums": [],
                    "records": [],
                    "properties": [],
                    "variables": [],
                    "imports": [],
                    "function_calls": [],
                    "is_dependency": is_dependency,
                    "lang": self.language_name,
                }

            tree = self.parser.parse(bytes(source_code, "utf8"))

            # Extract namespace declaration for package_name (e.g. "com.ea.nucleus.billing")
            namespace_match = re.search(r'\bnamespace\s+([\w.]+)', source_code)
            package_name = namespace_match.group(1) if namespace_match else None

            parsed_functions = []
            parsed_classes = []
            parsed_interfaces = []
            parsed_structs = []
            parsed_enums = []
            parsed_records = []
            parsed_properties = []
            parsed_variables = []
            parsed_imports = []
            parsed_calls = []

            for capture_name, query_str in CSHARP_QUERIES.items():
                captures = execute_query(self.language, query_str, tree.root_node)

                if capture_name == "functions":
                    parsed_functions = self._parse_functions(captures, source_code, path, tree.root_node)
                elif capture_name == "classes":
                    parsed_classes = self._parse_type_declarations(captures, source_code, path, "Class")
                elif capture_name == "interfaces":
                    parsed_interfaces = self._parse_type_declarations(captures, source_code, path, "Interface")
                elif capture_name == "structs":
                    parsed_structs = self._parse_type_declarations(captures, source_code, path, "Struct")
                elif capture_name == "enums":
                    parsed_enums = self._parse_type_declarations(captures, source_code, path, "Enum")
                elif capture_name == "records":
                    parsed_records = self._parse_type_declarations(captures, source_code, path, "Record")
                elif capture_name == "properties":
                    parsed_properties = self._parse_properties(captures, source_code, path, tree.root_node)
                elif capture_name == "variables":
                    parsed_variables = self._parse_variables(captures)
                elif capture_name == "imports":
                    parsed_imports = self._parse_imports(captures, source_code)
                elif capture_name == "calls":
                    parsed_calls = self._parse_calls(captures, source_code)

            return {
                "path": str(path),
                "functions": parsed_functions,
                "classes": parsed_classes,
                "interfaces": parsed_interfaces,
                "structs": parsed_structs,
                "enums": parsed_enums,
                "records": parsed_records,
                "properties": parsed_properties,
                "variables": parsed_variables,
                "imports": parsed_imports,
                "function_calls": parsed_calls,
                "is_dependency": is_dependency,
                "lang": self.language_name,
                "package_name": package_name,
            }

        except Exception as e:
            error_logger(f"Error parsing C# file {path}: {e}")
            return {
                "path": str(path),
                "functions": [],
                "classes": [],
                "interfaces": [],
                "structs": [],
                "enums": [],
                "records": [],
                "properties": [],
                "variables": [],
                "imports": [],
                "function_calls": [],
                "is_dependency": is_dependency,
                "lang": self.language_name,
            }

    def _parse_functions(self, captures: list, source_code: str, path: Path, root_node) -> list[Dict[str, Any]]:
        functions = []
        source_lines = source_code.splitlines()

        for node, capture_name in captures:
            if capture_name == "function_node":
                try:
                    start_line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    
                    name_captures = [
                        (n, cn) for n, cn in captures 
                        if cn == "name" and n.parent.id == node.id
                    ]
                    
                    if name_captures:
                        name_node = name_captures[0][0]
                        func_name = self._get_node_text(name_node)
                        
                        params_captures = [
                            (n, cn) for n, cn in captures 
                            if cn == "params" and n.parent.id == node.id
                        ]
                        
                        parameters = []
                        if params_captures:
                            params_node = params_captures[0][0]
                            parameters = self._extract_parameters(params_node)

                        # Extract attributes applied to this function.
                        # attribute_list nodes are CHILDREN of the declaration
                        # (never its parent), so the old parent check was
                        # always false and attributes stayed [] (#1538).
                        attributes = []
                        for child in node.children:
                            if child.type == "attribute_list":
                                for attr in child.children:
                                    if attr.type == "attribute":
                                        attributes.append(self._get_node_text(attr))

                        # Find containing class/struct/interface
                        class_context = self._find_containing_type(node, source_code)

                        source_text = self._get_node_text(node)
                        
                        func_data = {
                            "name": func_name,
                            "args": parameters,
                            "attributes": attributes,
                            # The declaration line, not the first attribute's
                            # (python.py's decorated_definition convention).
                            "line_number": name_node.start_point[0] + 1,
                            "end_line": end_line,
                            "path": str(path),
                            "lang": self.language_name,
                        }
                        
                        # Add class context if found
                        if class_context:
                            func_data["class_context"] = class_context

                        if self.index_source:
                            func_data["source"] = source_text
                        
                        functions.append(func_data)
                        
                except Exception as e:
                    error_logger(f"Error parsing function in {path}: {e}")
                    continue

        return functions

    def _parse_type_declarations(self, captures: list, source_code: str, path: Path, type_label: str) -> list[Dict[str, Any]]:
        """Parse class, interface, struct, enum, or record declarations with inheritance info."""
        types = []
        
        # Map capture names based on type
        capture_map = {
            "Class": "class",
            "Interface": "interface",
            "Struct": "struct",
            "Enum": "enum",
            "Record": "record"
        }
        expected_capture = capture_map.get(type_label, "class")

        for node, capture_name in captures:
            if capture_name == expected_capture:
                try:
                    start_line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    
                    name_captures = [
                        (n, cn) for n, cn in captures 
                        if cn == "name" and n.parent.id == node.id
                    ]
                    
                    if name_captures:
                        name_node = name_captures[0][0]
                        type_name = self._get_node_text(name_node)
                        
                        # Extract base classes/interfaces
                        bases = []
                        bases_captures = [
                            (n, cn) for n, cn in captures 
                            if cn == "bases" and n.parent.id == node.id
                        ]
                        
                        if bases_captures:
                            bases_node = bases_captures[0][0]
                            bases_text = self._get_node_text(bases_node)
                            # Parse base list: ": BaseClass, IInterface1, IInterface2"
                            bases_text = bases_text.strip().lstrip(':').strip()
                            if bases_text:
                                # Split on TOP-LEVEL commas only: a naive split
                                # shredded multi-arg generics —
                                # `: Dictionary<string, int>` became two bases,
                                # 'Dictionary<string' and 'int>', creating
                                # INHERITS edges to nonexistent types (#1538).
                                raw_bases = []
                                depth = 0
                                current = []
                                for ch in bases_text:
                                    if ch in "<([":
                                        depth += 1
                                    elif ch in ">)]":
                                        depth = max(0, depth - 1)
                                    if ch == "," and depth == 0:
                                        raw_bases.append("".join(current).strip())
                                        current = []
                                    else:
                                        current.append(ch)
                                if current:
                                    raw_bases.append("".join(current).strip())
                                # records might have base calls like : Person(name, age)
                                # we want just Person
                                for rb in raw_bases:
                                    base_name = rb.split('(')[0].strip()
                                    if base_name:
                                        bases.append(base_name)
                        
                        source_text = self._get_node_text(node)
                        
                        type_data = {
                            "name": type_name,
                            "line_number": start_line,
                            "end_line": end_line,
                            "path": str(path),
                            "lang": self.language_name,
                            "is_partial": bool(re.search(r"\bpartial\b", source_text)),
                        }
                        
                        # Add bases if found
                        if bases:
                            type_data["bases"] = bases

                        if self.index_source:
                            type_data["source"] = source_text
                        
                        types.append(type_data)
                        
                except Exception as e:
                    error_logger(f"Error parsing {type_label} in {path}: {e}")
                    continue

        return types

    def _parse_imports(self, captures: list, source_code: str) -> list[dict]:
        imports = []
        
        for node, capture_name in captures:
            if capture_name == "import":
                try:
                    import_text = self._get_node_text(node)
                    # Match: using System.Collections.Generic; or using static System.Math;
                    import_match = re.search(r'using\s+(?:static\s+)?([^;]+)', import_text)
                    if import_match:
                        import_path = import_match.group(1).strip()
                        
                        # Check for alias: using MyAlias = System.Collections.Generic.List<int>;
                        alias = None
                        if '=' in import_path:
                            parts = import_path.split('=')
                            alias = parts[0].strip()
                            import_path = parts[1].strip()
                        
                        import_data = {
                            "name": import_path,
                            "full_import_name": import_path,
                            "line_number": node.start_point[0] + 1,
                            "alias": alias,
                            # `global using X;` applies to the whole project;
                            # `using static X;` imports a type, not a namespace.
                            "is_global": import_text.lstrip().startswith("global"),
                            "is_static": bool(re.match(r'\s*(?:global\s+)?using\s+static\b', import_text)),
                            "context": (None, None),
                            "lang": self.language_name,
                            "is_dependency": False,
                        }
                        imports.append(import_data)
                except Exception as e:
                    error_logger(f"Error parsing import: {e}")
                    continue

        return imports

    def _parse_variables(self, captures: list) -> list:
        """Fields and locals, from `variable_declaration` nodes.

        Both shapes nest identically — `variable_declaration` carries the type
        and holds one `variable_declarator` per declared name, so
        `int a = 1, b = 2;` yields two rows sharing a type.
        """
        variables = []
        seen = set()
        for node, capture_name in captures:
            if capture_name != "decl":
                continue
            type_node = node.child_by_field_name("type")
            var_type = self._get_node_text(type_node) if type_node is not None else None

            for declarator in node.children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                if name_node is None:
                    name_node = next(
                        (c for c in declarator.children if c.type == "identifier"), None
                    )
                if name_node is None:
                    continue

                line_number = name_node.start_point[0] + 1
                name = self._get_node_text(name_node)
                if (name, line_number) in seen:
                    continue
                seen.add((name, line_number))

                # tree-sitter-c-sharp has no `equals_value_clause`: the
                # initialiser is a direct sibling following the `=` token.
                value = None
                seen_equals = False
                for child in declarator.children:
                    if child.type == "=":
                        seen_equals = True
                        continue
                    if seen_equals and child.is_named:
                        value = self._get_node_text(child)
                        break

                context, _ctx_type, _ctx_line = self._get_parent_context(
                    declarator, types=("method_declaration", "function_declaration")
                )
                class_context, _c_type, _c_line = self._get_parent_context(
                    declarator,
                    types=("class_declaration", "struct_declaration", "record_declaration"),
                )

                variables.append({
                    "name": name,
                    "line_number": line_number,
                    "value": value,
                    "type": var_type,
                    "context": context,
                    "class_context": class_context,
                    "lang": self.language_name,
                    "is_dependency": False,
                })
        return variables

    # Function nodes are recorded at their name's line (attributes such as
    # [HttpGet] come before it), type nodes at their declaration's first line:
    # a context must give the same line, or the writer cannot find the caller
    # node and drops the call (every attributed controller action / test lost
    # its outgoing CALLS).
    _FUNCTION_CONTEXTS = ('method_declaration', 'constructor_declaration', 'local_function_statement',
                          'function_declaration')

    def _get_parent_context(self, node: Any, types: Tuple[str, ...] = (
            'class_declaration', 'struct_declaration', 'record_declaration', 'interface_declaration',
            'function_declaration', 'method_declaration', 'constructor_declaration', 'local_function_statement')):
        """Find parent context for C# constructs: (name, node type, line of its graph node)."""
        curr = node.parent
        while curr:
            if curr.type in types:
                name_node = curr.child_by_field_name('name')
                if name_node:
                    anchor = name_node if curr.type in self._FUNCTION_CONTEXTS else curr
                    return self._get_node_text(name_node), curr.type, anchor.start_point[0] + 1
            curr = curr.parent
        return None, None, None

    def _get_node_text(self, node: Any) -> str:
        if not node: return ""
        return node.text.decode("utf-8")

    # ------------------------------------------------------------------
    # Receiver type inference: `IGreetingService svc = ...; svc.Greet()`
    # ------------------------------------------------------------------

    _SCOPE_TYPES = (
        "method_declaration", "constructor_declaration", "local_function_statement",
        "accessor_declaration", "lambda_expression", "operator_declaration",
    )
    _TYPE_DECL_TYPES = (
        "class_declaration", "struct_declaration", "record_declaration", "interface_declaration",
    )

    @staticmethod
    def _simple_type_name(type_text: str) -> Optional[str]:
        """`Ns.IRepo<User>?` -> `IRepo`; `var`, arrays and tuples -> None."""
        t = (type_text or "").strip()
        if not t or t == "var" or t.startswith("(") or t.endswith("]"):
            return None
        t = t.split("<", 1)[0].rstrip("?").strip()
        t = t.rsplit(".", 1)[-1].rsplit("::", 1)[-1]
        return t if t and (t[0].isalpha() or t[0] == "_") else None

    def _declared_names(self, node, out: Dict[str, str]) -> None:
        """Collect `name -> type` declared anywhere under *node* (no nested types)."""
        stack = [node]
        while stack:
            cur = stack.pop()
            ntype = cur.type
            if ntype in self._TYPE_DECL_TYPES and cur is not node:
                continue
            if ntype == "variable_declaration":
                type_node = cur.child_by_field_name("type")
                type_text = self._get_node_text(type_node) if type_node else ""
                for decl in cur.named_children:
                    if decl.type != "variable_declarator":
                        continue
                    name_node = decl.child_by_field_name("name") or next(
                        (c for c in decl.named_children if c.type == "identifier"), None)
                    if name_node is None:
                        continue
                    t = self._simple_type_name(type_text)
                    if t is None and type_text.strip() == "var":
                        creation = self._first_descendant(decl, "object_creation_expression")
                        if creation is not None and creation.child_by_field_name("type") is not None:
                            t = self._simple_type_name(self._get_node_text(creation.child_by_field_name("type")))
                    if t:
                        out.setdefault(self._get_node_text(name_node), t)
            elif ntype in ("parameter", "property_declaration", "foreach_statement"):
                type_node = cur.child_by_field_name("type")
                name_node = cur.child_by_field_name("name") or cur.child_by_field_name("left")
                if type_node is not None and name_node is not None and name_node.type == "identifier":
                    t = self._simple_type_name(self._get_node_text(type_node))
                    if t:
                        out.setdefault(self._get_node_text(name_node), t)
            stack.extend(reversed(cur.named_children))

    @staticmethod
    def _first_descendant(node, node_type: str):
        stack = list(node.named_children)
        while stack:
            cur = stack.pop(0)
            if cur.type == node_type:
                return cur
            stack.extend(cur.named_children)
        return None

    def _receiver_type(self, call_name_node, cache: Dict[int, Dict[str, str]]) -> Optional[str]:
        """Declared type of the receiver of `recv.Method(...)`, if knowable."""
        member = call_name_node.parent
        if member is None or member.type != "member_access_expression":
            return None
        receiver = member.child_by_field_name("expression")
        if receiver is None:
            return None
        if receiver.type == "member_access_expression":
            # this.field.Method()
            inner = receiver.child_by_field_name("expression")
            if inner is None or inner.type not in ("this", "this_expression"):
                return None
            receiver = receiver.child_by_field_name("name")
        if receiver is None or receiver.type != "identifier":
            return None
        name = self._get_node_text(receiver)
        # Innermost scope first (locals, parameters), then enclosing types
        # (fields, properties), so a local shadows a field of the same name.
        cur = member.parent
        while cur is not None:
            if cur.type in self._SCOPE_TYPES or cur.type in self._TYPE_DECL_TYPES:
                key = cur.id
                if key not in cache:
                    names: Dict[str, str] = {}
                    if cur.type in self._TYPE_DECL_TYPES:
                        body = cur.child_by_field_name("body") or cur
                        for child in body.named_children:
                            if child.type in ("field_declaration", "property_declaration",
                                              "event_field_declaration"):
                                self._declared_names(child, names)
                        params = cur.child_by_field_name("parameters")  # primary constructors
                        if params is not None:
                            self._declared_names(params, names)
                    else:
                        self._declared_names(cur, names)
                    cache[key] = names
                if name in cache[key]:
                    return cache[key][name]
            cur = cur.parent
        return None

    def _parse_calls(self, captures: list, source_code: str) -> list[dict]:
        calls = []
        seen_calls = set()
        scope_cache: Dict[int, Dict[str, str]] = {}
        
        # Build a map of node id to its full_name text
        full_names = {}
        for node, capture_name in captures:
            if capture_name == "full_name":
                full_names[node.id] = self._get_node_text(node)

        for node, capture_name in captures:
            if capture_name == "name":
                try:
                    call_name = self._get_node_text(node)
                    line_number = node.start_point[0] + 1
                    
                    # Dedupe per CALL SITE, not per (name, line): keying on
                    # name_line dropped distinct calls to the same function on
                    # one line — `Math.Max(Clamp(a), Clamp(b))` recorded ONE
                    # Clamp (#1538). The node's byte offset separates them.
                    call_key = (call_name, line_number, node.start_byte)
                    if call_key in seen_calls:
                        continue
                    seen_calls.add(call_key)
                    
                    # Check if this name is part of a member_access_expression
                    full_call_name = call_name
                    if node.parent and node.parent.id in full_names:
                        full_call_name = full_names[node.parent.id]
                    
                    # Get context
                    context_name, context_type, context_line = self._get_parent_context(node)
                    # A call's first enclosing declaration is its METHOD, so
                    # `'class' in context_type` was always false and every
                    # call's class_context was None (#1538). Walk again for
                    # class-like ancestors only, cpp-style.
                    cls_name, _cls_type, cls_line = self._get_parent_context(
                        node,
                        types=("class_declaration", "struct_declaration",
                               "record_declaration", "interface_declaration"),
                    )
                    class_context = (cls_name, cls_line) if cls_name else None

                    call_node = node.parent
                    if call_node is not None and call_node.type in ("member_access_expression", "qualified_name"):
                        call_node = call_node.parent
                    arg_list = call_node.child_by_field_name("arguments") if call_node is not None else None
                    args = [self._get_node_text(a) for a in arg_list.named_children if a.type == "argument"] if arg_list else []

                    call_data = {
                        "name": call_name,
                        "full_name": full_call_name,
                        "line_number": line_number,
                        "args": args,
                        "inferred_obj_type": self._receiver_type(node, scope_cache),
                        # `new T(...)`: the name is the created type, not a method.
                        "is_constructor": node.parent is not None
                        and node.parent.type == "object_creation_expression",
                        "context": (context_name, context_type, context_line),
                        "class_context": class_context,
                        "lang": self.language_name,
                        "is_dependency": False,
                    }
                    calls.append(call_data)
                except Exception as e:
                    error_logger(f"Error parsing call: {e}")
                    continue

        return calls

    

    def _extract_parameters(self, params_node) -> list[str]:
        params = []
        if not params_node:
            return params
            
        # Iterate over parameter nodes in the parameter list
        for child in params_node.children:
            if child.type == "parameter":
                # find the identifier
                name_node = child.child_by_field_name("name")
                if name_node:
                     params.append(self._get_node_text(name_node))
                else:
                    # Fallback: scan children for identifier if field name not present in this grammar version
                    for sub in child.children:
                        if sub.type == "identifier":
                            params.append(self._get_node_text(sub))
                            break
        return params

    def _find_containing_type(self, node, source_code):
        """Find the containing class, struct, interface, or record for a given node."""
        current = node.parent
        while current:
            if current.type in ['class_declaration', 'struct_declaration', 'interface_declaration', 'record_declaration']:
                # Find the name of this type
                for child in current.children:
                    if child.type == 'identifier':
                        return self._get_node_text(child)
            current = current.parent
        return None

    def _parse_properties(self, captures: list, source_code: str, path: Path, root_node) -> list[Dict[str, Any]]:
        """Parse C# properties."""
        properties = []
        
        for node, capture_name in captures:
            if capture_name == "property":
                try:
                    start_line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    
                    name_captures = [
                        (n, cn) for n, cn in captures 
                        if cn == "name" and n.parent == node
                    ]
                    
                    if name_captures:
                        name_node = name_captures[0][0]
                        prop_name = self._get_node_text(name_node)
                        
                        # Get property type from node children
                        prop_type = None
                        for child in node.children:
                            if child.type in ['predefined_type', 'identifier', 'generic_name', 'nullable_type', 'array_type']:
                                prop_type = self._get_node_text(child)
                                break
                        
                        
                        # Find containing class/struct
                        class_context = self._find_containing_type(node, source_code)
                        
                        source_text = self._get_node_text(node)
                        
                        prop_data = {
                            "name": prop_name,
                            "type": prop_type,
                            "line_number": start_line,
                            "end_line": end_line,
                            "path": str(path),
                            "lang": self.language_name,
                        }
                        
                        if class_context:
                            prop_data["class_context"] = class_context
                            
                        if self.index_source:
                            prop_data["source"] = source_text
                        
                        properties.append(prop_data)
                        
                except Exception as e:
                    error_logger(f"Error parsing property in {path}: {e}")
                    continue
        
        return properties



def pre_scan_csharp(files: list[Path], parser_wrapper) -> dict:
    """Pre-scan C# files to build a name-to-files mapping."""
    name_to_files = {}
    
    for path in files:
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            # Match class declarations
            class_matches = re.finditer(
                r'\b(?:public\s+|private\s+|protected\s+|internal\s+)?(?:static\s+)?(?:abstract\s+)?(?:sealed\s+)?(?:partial\s+)?class\s+(\w+)',
                content
            )
            for match in class_matches:
                class_name = match.group(1)
                if class_name not in name_to_files:
                    name_to_files[class_name] = []
                name_to_files[class_name].append(str(path))
            
            # Match interface declarations
            interface_matches = re.finditer(
                r'\b(?:public\s+|private\s+|protected\s+|internal\s+)?(?:partial\s+)?interface\s+(\w+)',
                content
            )
            for match in interface_matches:
                interface_name = match.group(1)
                if interface_name not in name_to_files:
                    name_to_files[interface_name] = []
                name_to_files[interface_name].append(str(path))
            
            # Match struct declarations
            struct_matches = re.finditer(
                r'\b(?:public\s+|private\s+|protected\s+|internal\s+)?(?:readonly\s+)?(?:partial\s+)?struct\s+(\w+)',
                content
            )
            for match in struct_matches:
                struct_name = match.group(1)
                if struct_name not in name_to_files:
                    name_to_files[struct_name] = []
                name_to_files[struct_name].append(str(path))
            
            # Match record declarations
            record_matches = re.finditer(
                r'\b(?:public\s+|private\s+|protected\s+|internal\s+)?(?:sealed\s+)?record\s+(?:class\s+)?(\w+)',
                content
            )
            for match in record_matches:
                record_name = match.group(1)
                if record_name not in name_to_files:
                    name_to_files[record_name] = []
                name_to_files[record_name].append(str(path))
                
        except Exception as e:
            error_logger(f"Error pre-scanning C# file {path}: {e}")
            
    return name_to_files
