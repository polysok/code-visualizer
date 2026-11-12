# src/code_visualizer/prompts.py
"""
This file contains the system prompt for the language model.
This prompt provides the core instructions, principles, and standard operating
procedures for the AI assistant, guiding it on how to effectively use the tools
provided by this MCP server.
"""

from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

LLM_SYSTEM_PROMPT = """# Code graph tools

This server gives you a graph of the developer's code base: every function and
type, who calls whom, inheritance, imports, and the calls into packages
(external:<Type> nodes, with their NuGet package when it is known). Other
repositories of the team may be indexed with it ("companion sources"): their
code is then real nodes too.

Ground your answers in it instead of guessing:

1. `graph_overview` when you do not know the code base yet.
2. `search_code` to turn a question, an issue or a stack trace into entities
   (keywords, identifiers or a description of what the code does).
3. `traverse_graph` from those entities: downstream to follow what they do,
   upstream to find who uses them (impact of a change, call sites).
4. `read_code` to read the entities that matter, with their exact location.
5. `execute_cypher_query` only for what the others cannot answer (counts,
   multi-step patterns).

Entity ids come from the tools: '<path>' (a file), '<path>:<Class.member>@<line>'
(a function or type), 'external:<Type>' (a package type). Pass them back as
they are. To locate the code an issue is about, search with its symptoms and
identifiers, walk from the hits, and rank the entities you read by how
directly they explain the issue.
"""


def build_system_prompt(project_root: Optional[Path] = None) -> str:
    """
    Build the complete system prompt by prepending custom prompt files.
    
    Args:
        project_root: Root directory of the project. If None, uses current directory.
    
    Returns:
        Complete system prompt with custom prompts prepended to the base prompt.
    """
    # Import here to avoid circular dependencies
    try:
        from code_visualizer.cli.project_config import get_prompt_file_contents
        
        # Get custom prompt contents
        custom_prompts = get_prompt_file_contents(project_root)
        
        if not custom_prompts:
            # No custom prompts, return base prompt
            return LLM_SYSTEM_PROMPT
        
        # Build combined prompt: custom prompts first, then base prompt
        combined_parts = []
        
        # Add custom prompts
        for i, custom_content in enumerate(custom_prompts, 1):
            combined_parts.append(f"# Custom Instructions {i}\n\n{custom_content}")
        
        # Add separator and base prompt
        combined_parts.append("---\n")
        combined_parts.append(LLM_SYSTEM_PROMPT)
        
        return "\n\n".join(combined_parts)
        
    except ImportError:
        # If project_config is not available (shouldn't happen in normal use)
        logger.warning("Could not import project_config module, using base prompt only")
        return LLM_SYSTEM_PROMPT
    except Exception as e:
        # Log error but continue with base prompt to maintain backward compatibility
        logger.warning(f"Error loading custom prompts: {e}. Using base prompt only.")
        return LLM_SYSTEM_PROMPT