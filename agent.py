#!/usr/bin/env python3
# This script implements the main command-line interface for the MCP Agent.
"""Base MCP Agent implementation with shared functionality."""

import asyncio
import json
import logging
import os
import subprocess
import sys
import signal
import termios
import tty
import select
import time
import re
from typing import Any, Dict, List, Optional, Union
from pathlib import Path
from abc import ABC, abstractmethod

import click

from fastmcp.client import Client as FastMCPClient, StdioTransport
import subprocess
import json

from config import HostConfig, load_config

# Configure logging
logging.basicConfig(
    level=logging.ERROR,  # Suppress WARNING messages during interactive chat
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SlashCommandManager:
    """Manages slash commands similar to Claude Code's system."""
    
    def __init__(self, agent: 'BaseMCPAgent'):
        self.agent = agent
        self.custom_commands = {}
        self.load_custom_commands()
    
    def load_custom_commands(self):
        """Load custom commands from .claude/commands/ and ~/.claude/commands/"""
        # Project-specific commands
        project_commands_dir = Path(".claude/commands")
        if project_commands_dir.exists():
            self._load_commands_from_dir(project_commands_dir, "project")
        
        # Personal commands
        personal_commands_dir = Path.home() / ".claude/commands"
        if personal_commands_dir.exists():
            self._load_commands_from_dir(personal_commands_dir, "personal")
    
    def _load_commands_from_dir(self, commands_dir: Path, command_type: str):
        """Load commands from a directory."""
        for command_file in commands_dir.glob("*.md"):
            try:
                with open(command_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                command_name = command_file.stem
                self.custom_commands[command_name] = {
                    "content": content,
                    "type": command_type,
                    "file": str(command_file)
                }
                logger.debug(f"Loaded {command_type} command: {command_name}")
            except Exception as e:
                logger.warning(f"Failed to load command {command_file}: {e}")
    
    async def handle_slash_command(self, command_line: str, messages: List[Dict[str, Any]] = None) -> Optional[str]:
        """Handle a slash command and return response if handled."""
        if not command_line.startswith('/'):
            return None
        
        # Parse command and arguments
        parts = command_line[1:].split(' ', 1)
        command = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        
        # Handle built-in commands
        if command == "help":
            return self._handle_help()
        elif command == "clear":
            return self._handle_clear()
        elif command == "compact":
            return await self._handle_compact(messages)
        elif command == "model":
            return self._handle_model(args)
        elif command == "review":
            return self._handle_review(args)
        elif command == "tokens":
            return self._handle_tokens(messages)
        elif command in ["quit", "exit"]:
            return self._handle_quit()
        elif command == "tools":
            return self._handle_tools()
        elif command == "switch-chat":
            return self._handle_switch_chat()
        elif command == "switch-reason":
            return self._handle_switch_reason()
        elif command == "switch-gemini":
            return self._handle_switch_gemini()
        elif command == "switch-gemini-pro":
            return self._handle_switch_gemini_pro()
        elif command.startswith("mcp__"):
            return await self._handle_mcp_command(command, args)
        elif ":" in command:
            # Custom namespaced command
            return await self._handle_custom_command(command, args)
        elif command in self.custom_commands:
            # Simple custom command
            return await self._handle_custom_command(command, args)
        else:
            return f"Unknown command: /{command}. Type /help for available commands."
    
    def _handle_help(self) -> str:
        """Handle /help command."""
        help_text = """Available Commands:

Built-in Commands:
  /help           - Show this help message
  /clear          - Clear conversation history
  /compact        - Compact conversation history into a summary
  /tokens         - Show current token usage statistics
  /model [name]   - Show current model or set model
  /review [file]  - Request code review
  /tools          - List all available tools
  /quit, /exit    - Exit the interactive chat

Model Switching:
  /switch-chat    - Switch to deepseek-chat model
  /switch-reason  - Switch to deepseek-reasoner model
  /switch-gemini  - Switch to Gemini Flash 2.5 backend
  /switch-gemini-pro - Switch to Gemini Pro 2.5 backend

Custom Commands:"""
        
        if self.custom_commands:
            for cmd_name, cmd_info in self.custom_commands.items():
                help_text += f"\n  /{cmd_name}         - {cmd_info['type']} command"
        else:
            help_text += "\n  (No custom commands found)"
        
        # Add MCP commands if available
        mcp_commands = self._get_mcp_commands()
        if mcp_commands:
            help_text += "\n\nMCP Commands:"
            for cmd in mcp_commands:
                help_text += f"\n  /{cmd}"
        
        return help_text
    
    def _handle_clear(self) -> Dict[str, Any]:
        """Handle /clear command."""
        if hasattr(self.agent, 'conversation_history'):
            self.agent.conversation_history.clear()
        return {"status": "Conversation history cleared.", "clear_messages": True}
    
    def _handle_quit(self) -> Dict[str, Any]:
        """Handle /quit and /exit commands."""
        return {"status": "Goodbye!", "quit": True}
    
    def _handle_tools(self) -> str:
        """Handle /tools command."""
        if not self.agent.available_tools:
            return "No tools available."
        
        tools_text = "Available tools:\n"
        for tool_name, tool_info in self.agent.available_tools.items():
            tools_text += f"  {tool_name}: {tool_info['description']}\n"
        
        return tools_text.rstrip()
    
    async def _handle_compact(self, messages: List[Dict[str, Any]] = None) -> str:
        """Handle /compact command."""
        if messages is None:
            return "No conversation history provided to compact."
        
        if len(messages) <= 3:
            return "Conversation is too short to compact (3 messages or fewer)."
        
        # Get token count before compacting
        if hasattr(self.agent, 'count_conversation_tokens'):
            tokens_before = self.agent.count_conversation_tokens(messages)
        else:
            tokens_before = "unknown"
        
        try:
            # Use the agent's compact_conversation method
            if hasattr(self.agent, 'compact_conversation'):
                compacted_messages = await self.agent.compact_conversation(messages)
                
                # Get token count after compacting
                if hasattr(self.agent, 'count_conversation_tokens'):
                    tokens_after = self.agent.count_conversation_tokens(compacted_messages)
                    result = f"✅ Conversation compacted: {len(messages)} → {len(compacted_messages)} messages\n📊 Token usage: ~{tokens_before} → ~{tokens_after} tokens"
                else:
                    result = f"✅ Conversation compacted: {len(messages)} → {len(compacted_messages)} messages"
                
                # Return both the result message and the compacted messages
                # The interactive chat will need to update its messages list
                return {"status": result, "compacted_messages": compacted_messages}
            else:
                return "❌ Conversation compacting not available for this agent type."
                
        except Exception as e:
            return f"❌ Failed to compact conversation: {str(e)}"
    
    def _handle_tokens(self, messages: List[Dict[str, Any]] = None) -> str:
        """Handle /tokens command."""
        if not hasattr(self.agent, 'count_conversation_tokens'):
            return "❌ Token counting not available for this agent type."
        
        if messages is None or len(messages) == 0:
            return "No conversation history to analyze."
        
        tokens = self.agent.count_conversation_tokens(messages)
        limit = self.agent.get_token_limit() if hasattr(self.agent, 'get_token_limit') else 32000
        percentage = (tokens / limit) * 100
        
        result = f"📊 Token usage: ~{tokens}/{limit} ({percentage:.1f}%)"
        if percentage > 80:
            result += "\n⚠️  Consider using '/compact' to reduce token usage"
        
        return result
    
    def _handle_switch_chat(self) -> Dict[str, Any]:
        """Handle /switch-chat command."""
        try:
            from config import load_config
            config = load_config()
            config.deepseek_model = "deepseek-chat"
            config.save()
            return {"status": f"✅ Model switched to: {config.deepseek_model}", "reload_host": "deepseek"}
        except Exception as e:
            return f"❌ Failed to switch model: {str(e)}"
    
    def _handle_switch_reason(self) -> Dict[str, Any]:
        """Handle /switch-reason command."""
        try:
            from config import load_config
            config = load_config()
            config.deepseek_model = "deepseek-reasoner"
            config.save()
            return {"status": f"✅ Model switched to: {config.deepseek_model}", "reload_host": "deepseek"}
        except Exception as e:
            return f"❌ Failed to switch model: {str(e)}"
    
    def _handle_switch_gemini(self) -> Dict[str, Any]:
        """Handle /switch-gemini command."""
        try:
            from config import load_config
            config = load_config()
            config.deepseek_model = "gemini"
            config.gemini_model = "gemini-2.5-flash"
            config.save()
            return {"status": f"✅ Backend switched to: Gemini Flash 2.5 ({config.gemini_model})", "reload_host": "gemini"}
        except Exception as e:
            return f"❌ Failed to switch backend: {str(e)}"
    
    def _handle_switch_gemini_pro(self) -> Dict[str, Any]:
        """Handle /switch-gemini-pro command."""
        try:
            from config import load_config
            config = load_config()
            config.deepseek_model = "gemini"
            config.gemini_model = "gemini-2.5-pro"
            config.save()
            return {"status": f"✅ Backend switched to: Gemini Pro 2.5 ({config.gemini_model})", "reload_host": "gemini"}
        except Exception as e:
            return f"❌ Failed to switch backend: {str(e)}"
    
    def _handle_model(self, args: str) -> str:
        """Handle /model command."""
        if not args.strip():
            # Show current model
            if hasattr(self.agent, 'config'):
                if hasattr(self.agent.config, 'get_deepseek_config'):
                    return f"Current model: {self.agent.config.get_deepseek_config().model}"
                elif hasattr(self.agent.config, 'get_gemini_config'):
                    return f"Current model: {self.agent.config.get_gemini_config().model}"
            return "Current model: Unknown"
        else:
            return "Model switching not implemented yet. Use environment variables to change models."
    
    def _handle_review(self, args: str) -> str:
        """Handle /review command."""
        if args.strip():
            file_path = args.strip()
            return f"Code review requested for: {file_path}\n\nNote: Automated code review not implemented yet. Please use the agent's normal chat to request code review."
        else:
            return "Please specify a file to review: /review <file_path>"
    
    async def _handle_mcp_command(self, command: str, args: str) -> str:
        """Handle MCP slash commands."""
        # Parse MCP command: mcp__<server-name>__<prompt-name>
        parts = command.split('__')
        if len(parts) != 3 or parts[0] != "mcp":
            return f"Invalid MCP command format: /{command}"
        
        server_name = parts[1]
        prompt_name = parts[2]
        
        # Check if we have this MCP server
        if hasattr(self.agent, 'available_tools'):
            # Look for matching tools from this server
            matching_tools = [tool for tool in self.agent.available_tools.keys() 
                             if tool.startswith(f"{server_name}:")]
            if not matching_tools:
                return f"MCP server '{server_name}' not found or has no available tools."
        
        return f"MCP command execution not fully implemented yet.\nServer: {server_name}\nPrompt: {prompt_name}\nArgs: {args}"
    
    async def _handle_custom_command(self, command: str, args: str) -> str:
        """Handle custom commands."""
        # Handle namespaced commands (prefix:command)
        if ":" in command:
            prefix, cmd_name = command.split(":", 1)
            full_command = command
        else:
            cmd_name = command
            full_command = command
        
        if cmd_name not in self.custom_commands:
            return f"Custom command not found: /{full_command}"
        
        cmd_info = self.custom_commands[cmd_name]
        content = cmd_info["content"]
        
        # Replace $ARGUMENTS placeholder
        if args:
            content = content.replace("$ARGUMENTS", args)
        else:
            content = content.replace("$ARGUMENTS", "")
        
        return f"Executing custom command '{cmd_name}':\n\n{content}"
    
    def _get_mcp_commands(self) -> List[str]:
        """Get available MCP commands."""
        mcp_commands = []
        if hasattr(self.agent, 'available_tools'):
            # Group tools by server and create MCP commands
            servers = set()
            for tool_name in self.agent.available_tools.keys():
                if ":" in tool_name and not tool_name.startswith("builtin:"):
                    server_name = tool_name.split(":")[0]
                    servers.add(server_name)
            
            for server in servers:
                mcp_commands.append(f"mcp__{server}__<prompt-name>")
        
        return mcp_commands


class InterruptibleInput:
    """Professional input handler using prompt_toolkit for robust terminal interaction."""
    
    def __init__(self):
        self.interrupted = False
        self._setup_prompt_toolkit()
    
    def _setup_prompt_toolkit(self):
        """Setup prompt_toolkit components."""
        try:
            from prompt_toolkit import prompt
            from prompt_toolkit.patch_stdout import patch_stdout
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.keys import Keys
            import asyncio
            
            self._prompt = prompt
            self._patch_stdout = patch_stdout
            self._available = True
            
            # Create key bindings for interruption
            self._bindings = KeyBindings()
            
            @self._bindings.add(Keys.Escape)
            def handle_escape(event):
                """Handle escape key for interruption when enabled."""
                if getattr(self, '_allow_escape_interrupt', False):
                    self.interrupted = True
                    event.app.exit(exception=KeyboardInterrupt)
            
        except ImportError:
            self._available = False
            logger.warning("prompt_toolkit not available, falling back to basic input")
    
    def get_input(self, prompt_text: str, multiline_mode: bool = False, allow_escape_interrupt: bool = False) -> Optional[str]:
        """Get input using prompt_toolkit for professional terminal interaction.
        
        Args:
            prompt_text: The prompt to display
            multiline_mode: If True, requires empty line to send. If False, sends on Enter.
            allow_escape_interrupt: If True, pressing ESC alone will interrupt. If False, ESC is ignored.
        """
        if not self._available:
            # Fallback to basic input if prompt_toolkit unavailable
            try:
                return input(prompt_text)
            except KeyboardInterrupt:
                self.interrupted = True
                return None
        
        try:
            # Set up escape interrupt behavior
            self._allow_escape_interrupt = allow_escape_interrupt
            
            # Check if we're in an asyncio event loop
            import asyncio
            
            try:
                # Try to get the current event loop
                loop = asyncio.get_running_loop()
                # We're in an async context, need to run in a thread
                import concurrent.futures
                import threading
                
                def run_prompt():
                    """Run prompt_toolkit in a separate thread to avoid event loop conflicts."""
                    return self._prompt(
                        prompt_text,
                        key_bindings=self._bindings if allow_escape_interrupt else None,
                        multiline=multiline_mode,
                        wrap_lines=True,
                        enable_history_search=False,
                    )
                
                # Run the prompt in a thread pool to avoid asyncio conflicts
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(run_prompt)
                    result = future.result()
                    return result
                    
            except RuntimeError:
                # No event loop running, safe to use prompt_toolkit directly
                result = self._prompt(
                    prompt_text,
                    key_bindings=self._bindings if allow_escape_interrupt else None,
                    multiline=multiline_mode,
                    wrap_lines=True,
                    enable_history_search=False,
                )
                return result
            
        except KeyboardInterrupt:
            self.interrupted = True
            return None
        except EOFError:
            # Handle Ctrl+D gracefully
            return None
        except Exception as e:
            logger.error(f"Error in prompt_toolkit input: {e}")
            # Fallback to basic input
            try:
                return input(prompt_text)
            except KeyboardInterrupt:
                self.interrupted = True
                return None
    
    def get_multiline_input(self, initial_prompt: str, allow_escape_interrupt: bool = False) -> Optional[str]:
        """Get input with smart multiline detection using prompt_toolkit."""
        if not self._available:
            # Fallback behavior
            try:
                return input(initial_prompt)
            except KeyboardInterrupt:
                self.interrupted = True
                return None
        
        # For normal chat, just use single-line input by default
        # Users can paste multiline content and it will be handled automatically
        user_input = self.get_input(initial_prompt, multiline_mode=False, allow_escape_interrupt=allow_escape_interrupt)
        return user_input


class BaseMCPAgent(ABC):
    """Base class for MCP agents with shared functionality."""
    
    def __init__(self, config: HostConfig, is_subagent: bool = False):
        self.config = config
        self.is_subagent = is_subagent
        self.mcp_clients: Dict[str, ClientSession] = {}
        self.available_tools: Dict[str, Dict] = {}
        self.conversation_history: List[Dict[str, Any]] = []
        
        # Task management for subprocess-based subagents
        self.running_tasks: Dict[str, Dict] = {}  # task_id -> task_info
        self.task_counter = 0
        self.current_batch_id = None  # Track batches of parallel tasks
        self.batch_counter = 0
        
        # Communication socket for subagent forwarding (set by parent process)
        self.comm_socket = None
        
        # Centralized subagent management system
        if not is_subagent:
            try:
                import sys
                import os
                # Add current directory to path for subagent import
                current_dir = os.path.dirname(os.path.abspath(__file__))
                if current_dir not in sys.path:
                    sys.path.insert(0, current_dir)
                from subagent import SubagentManager
                self.subagent_manager = SubagentManager(config)
                
                # Event-driven message handling
                self.subagent_message_queue = asyncio.Queue()
                self.subagent_manager.add_message_callback(self._on_subagent_message)
                logger.info("Initialized centralized subagent management system")
            except ImportError as e:
                logger.warning(f"Failed to import subagent manager: {e}")
                self.subagent_manager = None
                self.subagent_message_queue = None
        else:
            self.subagent_manager = None
            self.subagent_message_queue = None
        
        # Add built-in tools
        self._add_builtin_tools()
        
        # Initialize slash command manager
        self.slash_commands = SlashCommandManager(self)
        
        logger.info(f"Initialized Base MCP Agent with {len(self.available_tools)} built-in tools")
    
    def _add_builtin_tools(self):
        """Add built-in tools to the available tools."""
        builtin_tools = {
            "builtin:bash_execute": {
                "server": "builtin",
                "name": "bash_execute",
                "description": "Execute a bash command and return the output",
                "schema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The bash command to execute"},
                        "timeout": {"type": "integer", "default": 120, "description": "Timeout in seconds"}
                    },
                    "required": ["command"]
                },
                "client": None
            },
            "builtin:read_file": {
                "server": "builtin",
                "name": "read_file",
                "description": "Read contents of a file with line numbers",
                "schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the file to read"},
                        "offset": {"type": "integer", "description": "Line number to start from"},
                        "limit": {"type": "integer", "description": "Number of lines to read"}
                    },
                    "required": ["file_path"]
                },
                "client": None
            },
            "builtin:write_file": {
                "server": "builtin",
                "name": "write_file",
                "description": "Write content to a file",
                "schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the file to write"},
                        "content": {"type": "string", "description": "Content to write to the file"}
                    },
                    "required": ["file_path", "content"]
                },
                "client": None
            },
            "builtin:list_directory": {
                "server": "builtin",
                "name": "list_directory",
                "description": "List contents of a directory",
                "schema": {
                    "type": "object",
                    "properties": {
                        "directory_path": {"type": "string", "description": "Path to the directory to list"}
                    },
                    "required": ["directory_path"]
                },
                "client": None
            },
            "builtin:get_current_directory": {
                "server": "builtin",
                "name": "get_current_directory",
                "description": "Get the current working directory",
                "schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                },
                "client": None
            },
            "builtin:todo_read": {
                "server": "builtin",
                "name": "todo_read",
                "description": "Read the current todo list",
                "schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                },
                "client": None
            },
            "builtin:todo_write": {
                "server": "builtin",
                "name": "todo_write",
                "description": "Write/update the todo list",
                "schema": {
                    "type": "object",
                    "properties": {
                        "todos": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "content": {"type": "string"},
                                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                                    "priority": {"type": "string", "enum": ["low", "medium", "high"]}
                                },
                                "required": ["id", "content", "status", "priority"]
                            }
                        }
                    },
                    "required": ["todos"]
                },
                "client": None
            },
            "builtin:replace_in_file": {
                "server": "builtin",
                "name": "replace_in_file",
                "description": "Replace text in a file",
                "schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the file"},
                        "old_text": {"type": "string", "description": "Text to replace"},
                        "new_text": {"type": "string", "description": "New text to replace with"}
                    },
                    "required": ["file_path", "old_text", "new_text"]
                },
                "client": None
            },
            # "builtin:edit_file": {
            #     "server": "builtin",
            #     "name": "edit_file",
            #     "description": "Edit a file using unified diff format patches",
            #     "schema": {
            #         "type": "object",
            #         "properties": {
            #             "file_path": {"type": "string", "description": "Path to the file to edit"},
            #             "diff": {"type": "string", "description": "Unified diff format patch to apply to the file"}
            #         },
            #         "required": ["file_path", "diff"]
            #     },
            #     "client": None
            # },
            "builtin:webfetch": {
                "server": "builtin",
                "name": "webfetch",
                "description": "Fetch content from a webpage",
                "schema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to fetch"},
                        "limit": {"type": "integer", "description": "Optional limit to truncate the HTML response by this number of lines (default: 1000)"}
                    },
                    "required": ["url"]
                },
                "client": None
            },
            "builtin:task": {
                "server": "builtin",
                "name": "task",
                "description": "Spawn a subagent to investigate a specific task and return a comprehensive summary. IMPORTANT: To spawn multiple subagents simultaneously, make multiple tool calls to 'builtin_task' in the same response - do not wait for results between calls. The main agent will automatically pause after spawning subagents, wait for all to complete, then restart with their combined results.",
                "schema": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string", "description": "A brief description of the task (3-5 words)"},
                        "prompt": {"type": "string", "description": "Detailed instructions for what the subagent should investigate or accomplish"},
                        "context": {"type": "string", "description": "Optional additional context or files the subagent should consider"}
                    },
                    "required": ["description", "prompt"]
                },
                "client": None
            },
            "builtin:task_status": {
                "server": "builtin",
                "name": "task_status",
                "description": "Check the status of running subagent tasks",
                "schema": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "Optional specific task ID to check. If not provided, shows all tasks"}
                    }
                },
                "client": None
            },
            "builtin:task_results": {
                "server": "builtin",
                "name": "task_results",
                "description": "Retrieve the results and summaries from completed subagent tasks",
                "schema": {
                    "type": "object",
                    "properties": {
                        "include_running": {"type": "boolean", "description": "Whether to include running tasks (default: false, only completed)"},
                        "clear_after_retrieval": {"type": "boolean", "description": "Whether to clear tasks after retrieving results (default: true)"}
                    }
                },
                "client": None
            }
        }
        
        self.available_tools.update(builtin_tools)
    
    async def _execute_builtin_tool(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Execute a built-in tool."""
        if tool_name == "bash_execute":
            return self._bash_execute(args)
        elif tool_name == "read_file":
            return self._read_file(args)
        elif tool_name == "write_file":
            return self._write_file(args)
        elif tool_name == "list_directory":
            return self._list_directory(args)
        elif tool_name == "get_current_directory":
            return self._get_current_directory(args)
        elif tool_name == "todo_read":
            return self._todo_read(args)
        elif tool_name == "todo_write":
            return self._todo_write(args)
        elif tool_name == "replace_in_file":
            return self._replace_in_file(args)
        # elif tool_name == "edit_file":
        #     return self._edit_file(args)
        elif tool_name == "webfetch":
            return self._webfetch(args)
        elif tool_name == "task":
            return await self._task(args)
        elif tool_name == "task_status":
            return self._task_status(args)
        elif tool_name == "task_results":
            return self._task_results(args)
        else:
            return f"Unknown built-in tool: {tool_name}"
    
    def _bash_execute(self, args: Dict[str, Any]) -> str:
        """Execute a bash command and return the output."""
        command = args.get("command", "")
        timeout = args.get("timeout", 120)
        
        if not command:
            return "Error: No command provided"
        
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            
            output = ""
            if result.stdout:
                output += f"STDOUT:\n{result.stdout}"
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr}"
            if result.returncode != 0:
                output += f"\nReturn code: {result.returncode}"
            
            return output if output else "Command executed successfully (no output)"
            
        except subprocess.TimeoutExpired:
            return f"Error: Command timed out after {timeout} seconds"
        except Exception as e:
            return f"Error executing command: {str(e)}"
    
    def _read_file(self, args: Dict[str, Any]) -> str:
        """Read contents of a file with line numbers."""
        file_path = args.get("file_path", "")
        offset = args.get("offset", 1)
        limit = args.get("limit", None)
        
        if not file_path:
            return "Error: No file path provided"
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            start_idx = max(0, offset - 1)  # Convert to 0-based index
            end_idx = len(lines) if limit is None else min(len(lines), start_idx + limit)
            
            result = []
            for i in range(start_idx, end_idx):
                result.append(f"{i + 1:6d}→{lines[i].rstrip()}")
            
            return "\n".join(result)
            
        except FileNotFoundError:
            return f"Error: File not found: {file_path}"
        except Exception as e:
            return f"Error reading file: {str(e)}"
    
    def _write_file(self, args: Dict[str, Any]) -> str:
        """Write content to a file."""
        file_path = args.get("file_path", "")
        content = args.get("content", "")
        
        if not file_path:
            return "Error: No file path provided"
        
        try:
            # Create directory if it doesn't exist
            dir_path = os.path.dirname(file_path)
            if dir_path:  # Only create directory if it's not empty (i.e., file is not in current dir)
                os.makedirs(dir_path, exist_ok=True)
            
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            return f"Successfully wrote {len(content)} characters to {file_path}"
            
        except Exception as e:
            return f"Error writing file: {str(e)}"
    
    def _list_directory(self, args: Dict[str, Any]) -> str:
        """List contents of a directory."""
        directory_path = args.get("directory_path", ".")
        
        try:
            path = Path(directory_path)
            if not path.exists():
                return f"Error: Directory does not exist: {directory_path}"
            
            if not path.is_dir():
                return f"Error: Path is not a directory: {directory_path}"
            
            items = []
            for item in sorted(path.iterdir()):
                if item.is_dir():
                    items.append(f"📁 {item.name}/")
                else:
                    size = item.stat().st_size
                    items.append(f"📄 {item.name} ({size} bytes)")
            
            return "\n".join(items) if items else "Directory is empty"
            
        except Exception as e:
            return f"Error listing directory: {str(e)}"
    
    def _get_current_directory(self, args: Dict[str, Any]) -> str:
        """Get the current working directory."""
        try:
            return os.getcwd()
        except Exception as e:
            return f"Error getting current directory: {str(e)}"
    
    def _todo_read(self, args: Dict[str, Any]) -> str:
        """Read the current todo list."""
        todo_file = "todo.json"
        
        try:
            if not os.path.exists(todo_file):
                return "[]"  # Empty todo list
            
            with open(todo_file, 'r', encoding='utf-8') as f:
                return f.read()
                
        except Exception as e:
            return f"Error reading todo list: {str(e)}"
    
    def _todo_write(self, args: Dict[str, Any]) -> str:
        """Write/update the todo list."""
        todos = args.get("todos", [])
        todo_file = "todo.json"
        
        try:
            with open(todo_file, 'w', encoding='utf-8') as f:
                json.dump(todos, f, indent=2)
            
            return f"Successfully updated todo list with {len(todos)} items"
            
        except Exception as e:
            return f"Error writing todo list: {str(e)}"
    
    def _replace_in_file(self, args: Dict[str, Any]) -> str:
        """Replace text in a file."""
        file_path = args.get("file_path", "")
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")
        
        if not file_path:
            return "Error: No file path provided"
        if not old_text:
            return "Error: No old text provided"
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            if old_text not in content:
                return f"Error: Text not found in file: {old_text}"
            
            new_content = content.replace(old_text, new_text)
            
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            
            count = content.count(old_text)
            return f"Successfully replaced {count} occurrence(s) of text in {file_path}"
            
        except FileNotFoundError:
            return f"Error: File not found: {file_path}"
        except Exception as e:
            return f"Error replacing text in file: {str(e)}"
    
    def _edit_file(self, args: Dict[str, Any]) -> str:
        """Edit a file using unified diff format."""
        file_path = Path(args["file_path"]).resolve()
        diff_content = args["diff"]
        
        try:
            if not file_path.exists():
                return f"Error: File does not exist: {file_path}"
            
            # Read the original file
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                original_lines = f.readlines()
            
            # Unescape JSON escape sequences in the diff content
            # This handles cases where Deepseek escapes \n, \", etc.
            try:
                # Try to decode JSON escape sequences
                import codecs
                unescaped_diff = codecs.decode(diff_content, 'unicode_escape')
            except:
                # If that fails, just use the original content
                unescaped_diff = diff_content
            
            # Debug: log the diff content and first few lines
            logger.warning(f"Applying diff to {file_path}")
            logger.warning(f"Original diff content: {repr(diff_content[:200])}")
            logger.warning(f"Unescaped diff content: {repr(unescaped_diff[:200])}")
            logger.warning(f"First 3 original lines: {[repr(line) for line in original_lines[:3]]}")
            
            # Parse and apply the diff
            modified_lines = self._apply_diff(original_lines, unescaped_diff)
            
            if modified_lines is None:
                return "Error: Failed to apply diff - invalid diff format or patch doesn't match file content"
            
            # Write the modified content back
            with open(file_path, 'w', encoding='utf-8') as f:
                f.writelines(modified_lines)
            
            return f"Successfully applied diff to {file_path}. Modified {len(modified_lines)} lines."
            
        except Exception as e:
            return f"Error editing file: {str(e)}"
    
    def _apply_diff(self, original_lines: List[str], diff_content: str) -> Optional[List[str]]:
        """Apply a unified diff to the original lines."""
        import re
        
        # Parse the diff into hunks
        hunks = []
        current_hunk = None
        
        for line in diff_content.split('\n'):
            line = line.rstrip('\r\n')
            
            # Look for hunk headers (@@)
            hunk_match = re.match(r'^@@\s*-(\d+)(?:,(\d+))?\s*\+(\d+)(?:,(\d+))?\s*@@', line)
            if hunk_match:
                if current_hunk:
                    hunks.append(current_hunk)
                
                old_start = int(hunk_match.group(1)) - 1  # Convert to 0-based indexing
                old_count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
                new_start = int(hunk_match.group(3)) - 1  # Convert to 0-based indexing
                new_count = int(hunk_match.group(4)) if hunk_match.group(4) else 1
                
                current_hunk = {
                    'old_start': old_start,
                    'old_count': old_count,
                    'new_start': new_start,
                    'new_count': new_count,
                    'lines': []
                }
            elif current_hunk is not None:
                # Process diff lines
                if line.startswith(' '):
                    current_hunk['lines'].append(('context', line[1:] + '\n'))
                elif line.startswith('-'):
                    current_hunk['lines'].append(('remove', line[1:] + '\n'))
                elif line.startswith('+'):
                    current_hunk['lines'].append(('add', line[1:] + '\n'))
                elif line.startswith('\\'):
                    # Handle "No newline at end of file" markers
                    continue
        
        if current_hunk:
            hunks.append(current_hunk)
        
        if not hunks:
            return None
        
        # Apply hunks in reverse order to preserve line numbers
        result_lines = original_lines.copy()
        
        for hunk in reversed(hunks):
            old_start = hunk['old_start']
            old_count = hunk['old_count']
            
            # Verify the context matches
            context_check = []
            add_lines = []
            remove_count = 0
            
            for action, content in hunk['lines']:
                if action == 'context':
                    context_check.append(content)
                elif action == 'remove':
                    context_check.append(content)
                    remove_count += 1
                elif action == 'add':
                    add_lines.append(content)
            
            # Check if the original content matches what we expect to remove
            try:
                original_section = result_lines[old_start:old_start + old_count]
                context_index = 0
                
                for action, content in hunk['lines']:
                    if action in ['context', 'remove']:
                        if context_index >= len(original_section):
                            logger.warning(f"Diff context mismatch at line {old_start + context_index + 1}: reached end of file")
                            logger.warning(f"Expected: {repr(content)}")
                            return None
                        elif original_section[context_index] != content:
                            logger.warning(f"Diff context mismatch at line {old_start + context_index + 1}")
                            logger.warning(f"Expected: {repr(content)}")
                            logger.warning(f"Found: {repr(original_section[context_index])}")
                            return None
                        context_index += 1
                
                # Apply the changes
                new_section = []
                for action, content in hunk['lines']:
                    if action in ['context', 'add']:
                        new_section.append(content)
                
                # Replace the section
                result_lines[old_start:old_start + old_count] = new_section
                
            except IndexError:
                return None
        
        return result_lines
    
    def _webfetch(self, args: Dict[str, Any]) -> str:
        """Fetch a webpage using curl and return its content."""
        url = args.get("url", "")
        limit = args.get("limit", 1000)  # Default to 1000 lines

        if not url:
            return "Error: No URL provided"

        # Use curl to fetch the webpage with a timeout, capturing raw output
        result = subprocess.run(
            ["curl", "-L", "--max-time", "30", url],
            capture_output=True,
            timeout=35  # Slightly longer than curl timeout
        )

        # Try to decode with utf-8, then fall back to latin-1 (which rarely fails)
        try:
            content = result.stdout.decode('utf-8')
        except UnicodeDecodeError:
            logger.warning(f"UTF-8 decoding failed for {url}. Falling back to latin-1.")
            content = result.stdout.decode('latin-1', errors='replace')

        if result.returncode != 0:
            # Try to decode stderr for a better error message
            try:
                stderr = result.stderr.decode('utf-8', errors='replace')
            except:
                stderr = repr(result.stderr)
            
            error_msg = f"Error fetching URL (curl return code {result.returncode}): {stderr}"
            
            # If we have content despite the error, include it
            if content.strip():
                return f"{error_msg}\n\nContent retrieved:\n{content}"
            else:
                return error_msg

        # Truncate the content by lines if limit is specified
        if limit is not None and isinstance(limit, int) and limit > 0:
            lines = content.split('\n')
            if len(lines) > limit:
                content = '\n'.join(lines[:limit])
                content += f"\n\n[Content truncated at {limit} lines. Original had {len(lines)} lines.]"

        # Return the content (truncated if limit was provided)
        return content
    
    async def _task(self, args: Dict[str, Any]) -> str:
        """Spawn a subagent subprocess to investigate a task and return immediately."""
        description = args.get("description", "")
        prompt = args.get("prompt", "")
        context = args.get("context", "")
        
        if not description:
            return "Error: No task description provided"
        if not prompt:
            return "Error: No task prompt provided"
        
        try:
            # Generate unique task ID and batch tracking
            self.task_counter += 1
            task_id = f"task_{self.task_counter}"
            
            # Create a new batch if this is the first task in a while
            # (assumes tasks called within 5 seconds are part of the same batch)
            current_time = time.time()
            if (self.current_batch_id is None or 
                not self.running_tasks or 
                current_time - max(task['start_time'] for task in self.running_tasks.values()) > 5):
                self.batch_counter += 1
                self.current_batch_id = f"batch_{self.batch_counter}"
            
            # Create task prompt
            task_prompt = f"""You are a specialized subagent tasked with investigating and completing the following task:

TASK: {description}

INSTRUCTIONS:
{prompt}

IMPORTANT: After completing your investigation/task, provide a clear and concise summary of your findings. Your summary should include:
1. What you investigated or accomplished
2. Key findings or results
3. Any important observations or insights
4. Conclusions or recommendations if applicable

Please structure your response so that the main findings are easily extractable for analysis."""
            
            if context:
                task_prompt += f"""

ADDITIONAL CONTEXT:
{context}"""
            
            task_prompt += """

Your goal is to thoroughly investigate this task using all available tools and provide a comprehensive summary of your findings. Be systematic, thorough, and provide actionable insights.

IMPORTANT: 
- Use tools extensively to gather information
- Provide a clear, well-structured summary
- Include specific details and findings
- If you encounter any issues, document them clearly
- Your response will be returned to the parent agent, so make it complete and self-contained"""
            
            # Write task prompt to a temporary file
            import tempfile
            import json
            import sys
            import os
            
            # Create a communication socket for tool execution forwarding
            import socket
            comm_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            comm_socket.bind(('localhost', 0))  # Bind to any available port
            comm_port = comm_socket.getsockname()[1]
            comm_socket.listen(1)
            
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as task_file:
                task_data = {
                    "task_id": task_id,
                    "description": description,
                    "prompt": task_prompt,
                    "timestamp": time.time(),
                    "comm_port": comm_port  # Add communication port
                }
                json.dump(task_data, task_file)
                task_file_path = task_file.name
            
            # Start subprocess for subagent
            python_executable = sys.executable
            script_path = os.path.abspath(__file__)
            
            # Start subprocess with task execution
            process = await asyncio.create_subprocess_exec(
                python_executable, script_path, "execute-task", task_file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.getcwd()
            )
            
            # Store task info
            self.running_tasks[task_id] = {
                "description": description,
                "process": process,
                "task_file": task_file_path,
                "result_file": task_file_path.replace('.json', '_result.json'),
                "start_time": current_time,
                "output_buffer": "",
                "completed": False,
                "result": None,
                "batch_id": self.current_batch_id,
                "comm_socket": comm_socket,
                "comm_port": comm_port
            }
            
            # Start monitoring task in background
            asyncio.create_task(self._monitor_task(task_id))
            
            # Start communication handler for tool execution forwarding
            asyncio.create_task(self._handle_subagent_communication(task_id))
            
            # Return immediately to allow main LLM to continue
            print(f"\n\r🤖 [SUBAGENT] Starting task {task_id}: {description}\r")
            print(f"🎯 [SUBAGENT] Task running in subprocess...\r")
            
            return f"""[SUBAGENT TASK STARTED]
Task ID: {task_id}
Description: {description}
Status: Running in subprocess

The subagent is now running independently and will report progress. You can continue with other tasks while this completes."""
            
        except Exception as e:
            logger.error(f"Error starting subagent task: {e}")
            return f"Error starting subagent task '{description}': {str(e)}"
    
    async def _monitor_task(self, task_id: str):
        """Monitor a running task subprocess and display its output."""
        if task_id not in self.running_tasks:
            return
        
        task_info = self.running_tasks[task_id]
        process = task_info["process"]
        
        try:
            import os
            
            # Monitor stdout and stderr
            while True:
                # Check if process is still running
                if process.returncode is not None:
                    break
                
                # Read available output with shorter timeout for real-time display
                try:
                    stdout_data = await asyncio.wait_for(process.stdout.read(1024), timeout=0.01)
                    if stdout_data:
                        output = stdout_data.decode('utf-8', errors='ignore')
                        # Store output for buffer
                        task_info["output_buffer"] += output
                        
                        # Store output for streaming integration
                        if "display_messages" not in task_info:
                            task_info["display_messages"] = []
                        task_info["display_messages"].append(f"🤖 [SUBAGENT {task_id}] {output}")
                        
                        # Print immediately for non-streaming contexts (fallback)
                        formatted_output = output.replace('\n', '\n\r')
                        print(f"🤖 [SUBAGENT {task_id}] {formatted_output}", end='', flush=True)
                except asyncio.TimeoutError:
                    pass
                
                # Shorter sleep for more responsive output
                await asyncio.sleep(0.01)
            
            # Process completed, get final output
            try:
                stdout, stderr = await process.communicate()
                if stdout:
                    output = stdout.decode('utf-8', errors='ignore')
                    formatted_output = output.replace('\n', '\n\r')
                    print(f"{formatted_output}", end='', flush=True)
                    task_info["output_buffer"] += output
                
                if stderr and stderr.strip():
                    error_output = stderr.decode('utf-8', errors='ignore')
                    print(f"\n\r🤖 [SUBAGENT ERROR {task_id}]: {error_output}\r")
            except Exception as e:
                print(f"\n\r🤖 [SUBAGENT ERROR {task_id}]: Failed to read final output: {e}\r")
            
            # Mark task as completed
            task_info["completed"] = True
            task_info["end_time"] = time.time()
            
            # Try to collect the result
            try:
                import json
                result_file = task_info["result_file"]
                if os.path.exists(result_file):
                    with open(result_file, 'r') as f:
                        result_data = json.load(f)
                        task_info["result"] = result_data.get("result", "No result captured")
                    # Clean up result file
                    os.unlink(result_file)
                else:
                    task_info["result"] = "Result file not found"
            except Exception as e:
                task_info["result"] = f"Error collecting result: {e}"
            
            print(f"\n\r🤖 [SUBAGENT] Task {task_id} completed: {task_info['description']}\r")
            
            # Check if all tasks in the current batch are completed
            self._check_all_tasks_completed()
            
            # Clean up task file
            try:
                os.unlink(task_info["task_file"])
            except Exception:
                pass
                
        except Exception as e:
            logger.error(f"Error monitoring task {task_id}: {e}")
            print(f"\n\r🤖 [SUBAGENT ERROR {task_id}]: Monitoring failed: {e}\r")
    
    def get_pending_subagent_messages(self):
        """Get all pending display messages from running subagents."""
        messages = []
        for task_id, task_info in self.running_tasks.items():
            if "display_messages" in task_info and task_info["display_messages"]:
                messages.extend(task_info["display_messages"])
                task_info["display_messages"].clear()  # Clear after retrieving
        return messages
    
    async def _handle_subagent_communication(self, task_id: str):
        """Handle communication from subagent for tool execution forwarding."""
        if task_id not in self.running_tasks:
            return
        
        import socket
        import json
        
        task_info = self.running_tasks[task_id]
        comm_socket = task_info["comm_socket"]
        
        try:
            # Wait for subagent to connect with timeout
            comm_socket.settimeout(10.0)  # 10 second timeout for connection
            try:
                # Use asyncio to make the blocking accept() non-blocking for the event loop
                loop = asyncio.get_event_loop()
                client_socket, addr = await loop.run_in_executor(None, comm_socket.accept)
                logger.debug(f"Subagent {task_id} connected for communication")
            except socket.timeout:
                logger.warning(f"Subagent {task_id} did not connect within timeout")
                return
            
            # Handle messages from subagent
            with client_socket:
                client_socket.settimeout(1.0)  # 1 second timeout for messages
                buffer = ""
                
                # Store client socket reference for sending responses
                task_info["client_socket"] = client_socket
                
                while task_id in self.running_tasks and not self.running_tasks[task_id].get("completed", False):
                    try:
                        # Use executor to make recv non-blocking for event loop
                        data = await loop.run_in_executor(None, client_socket.recv, 4096)
                        if not data:
                            break
                        data = data.decode('utf-8')
                        
                        buffer += data
                        
                        # Process complete messages (newline-delimited JSON)
                        while '\n' in buffer:
                            line, buffer = buffer.split('\n', 1)
                            if line.strip():
                                try:
                                    message = json.loads(line.strip())
                                    await self._process_subagent_message(task_id, message)
                                except json.JSONDecodeError as e:
                                    logger.error(f"Invalid JSON from subagent {task_id}: {e}")
                    
                    except socket.timeout:
                        # Continue waiting for more messages
                        continue
                    except Exception as e:
                        logger.error(f"Error receiving from subagent {task_id}: {e}")
                        break
                        
        except Exception as e:
            logger.error(f"Error in subagent communication for {task_id}: {e}")
        finally:
            try:
                comm_socket.close()
            except:
                pass
    
    async def _process_subagent_message(self, task_id: str, message: dict):
        """Process a message from a subagent."""
        try:
            msg_type = message.get("type")
            
            if msg_type == "tool_execution_request":
                # Handle tool execution request from subagent (don't await - run concurrently)
                asyncio.create_task(self._handle_subagent_tool_request(task_id, message))
                
            elif msg_type == "display_message":
                # Handle display messages from subagent - store for streaming integration
                display_msg = message.get("message", "")
                # Store in task info for streaming integration
                if task_id in self.running_tasks:
                    task_info = self.running_tasks[task_id]
                    if "display_messages" not in task_info:
                        task_info["display_messages"] = []
                    task_info["display_messages"].append(display_msg)
                # Also print immediately for non-streaming contexts
                print(display_msg, flush=True)
                
            elif msg_type == "tool_execution":
                # Forward tool execution to main chat
                tool_name = message.get("tool_name", "unknown")
                tool_args = message.get("tool_args", {})
                tool_result = message.get("tool_result", "")
                
                # Display tool execution in main chat
                print(f"\n\r🔧 [SUBAGENT {task_id}] Executing tool: {tool_name}\r")
                if tool_args:
                    print(f"📝 [SUBAGENT {task_id}] Arguments: {str(tool_args)[:100]}{'...' if len(str(tool_args)) > 100 else ''}\r")
                if tool_result:
                    # Truncate long results for display
                    result_preview = str(tool_result)[:300] + "..." if len(str(tool_result)) > 300 else str(tool_result)
                    print(f"✅ [SUBAGENT {task_id}] Result: {result_preview}\r")
                    
            elif msg_type == "status":
                # Handle status updates
                status = message.get("status", "")
                print(f"\n\r📊 [SUBAGENT {task_id}] {status}\r")
                
            elif msg_type == "error":
                # Handle error messages
                error = message.get("error", "")
                print(f"\n\r❌ [SUBAGENT {task_id}] Error: {error}\r")
                
        except Exception as e:
            logger.error(f"Error processing subagent message from {task_id}: {e}")
    
    async def _handle_subagent_tool_request(self, task_id: str, message: dict):
        """Handle a tool execution request from a subagent."""
        try:
            import json
            import asyncio
            
            request_id = message.get("request_id")
            tool_key = message.get("tool_key")
            tool_name = message.get("tool_name")
            tool_args = message.get("tool_args", {})
            
            if not request_id or not tool_key or not tool_name:
                logger.error(f"Invalid tool request from subagent {task_id}: missing required fields")
                return
            
            # Get the subagent's communication socket
            if task_id not in self.running_tasks:
                logger.error(f"Task {task_id} not found for tool request")
                return
                
            task_info = self.running_tasks[task_id]
            comm_socket = task_info.get("comm_socket")
            if not comm_socket:
                logger.error(f"No communication socket for task {task_id}")
                return
            
            # Display tool execution in main chat
            print(f"\n🔧 [SUBAGENT {task_id}] Executing tool: {tool_name}", flush=True)
            if tool_args:
                args_preview = str(tool_args)[:100] + "..." if len(str(tool_args)) > 100 else str(tool_args)
                print(f"📝 [SUBAGENT {task_id}] Arguments: {args_preview}", flush=True)
            
            # Execute the tool on behalf of the subagent
            try:
                # Temporarily disable subagent mode to execute locally
                original_is_subagent = self.is_subagent
                self.is_subagent = False
                
                result = await self._execute_mcp_tool(tool_key, tool_args)
                
                # Restore subagent mode
                self.is_subagent = original_is_subagent
                
                # Display result in main chat
                result_preview = str(result)[:300] + "..." if len(str(result)) > 300 else str(result)
                print(f"✅ [SUBAGENT {task_id}] Result: {result_preview}", flush=True)
                
                # Send successful response back to subagent (with full result so it can continue)
                response = {
                    "type": "tool_execution_response", 
                    "request_id": request_id,
                    "success": True,
                    "result": result  # Full result for subagent to continue its work
                }
                
            except SystemExit as e:
                # Handle Click's sys.exit() gracefully
                if e.code == 0:
                    # Successful exit, treat as success
                    result = "Command completed successfully"
                    result_preview = str(result)[:300] + "..." if len(str(result)) > 300 else str(result)
                    print(f"✅ [SUBAGENT {task_id}] Result: {result_preview}", flush=True)
                    
                    response = {
                        "type": "tool_execution_response", 
                        "request_id": request_id,
                        "success": True,
                        "result": result
                    }
                else:
                    # Non-zero exit, treat as error
                    error_msg = f"Tool exited with code {e.code}"
                    logger.error(f"Tool execution error for subagent {task_id}: {error_msg}")
                    print(f"❌ [SUBAGENT {task_id}] Tool error: {error_msg}", flush=True)
                    
                    response = {
                        "type": "tool_execution_response",
                        "request_id": request_id,
                        "success": False,
                        "error": error_msg
                    }
            except Exception as e:
                error_msg = f"Error executing tool {tool_name}: {str(e)}"
                logger.error(f"Tool execution error for subagent {task_id}: {e}")
                
                # Display error in main chat
                print(f"❌ [SUBAGENT {task_id}] Tool error: {error_msg}", flush=True)
                
                # Send error response back to subagent
                response = {
                    "type": "tool_execution_response",
                    "request_id": request_id,
                    "success": False,
                    "error": error_msg
                }
            
            # Send response back to subagent through client socket
            try:
                client_socket = task_info.get("client_socket")
                if client_socket:
                    response_json = json.dumps(response) + "\n"
                    client_socket.send(response_json.encode('utf-8'))
                else:
                    logger.error(f"No client socket available for subagent {task_id}")
                
            except Exception as e:
                logger.error(f"Error sending response to subagent {task_id}: {e}")
                
        except Exception as e:
            logger.error(f"Error handling tool request from subagent {task_id}: {e}")
    
    def _task_status(self, args: Dict[str, Any]) -> str:
        """Check the status of running subagent tasks."""
        task_id = args.get("task_id", None)
        
        if not self.running_tasks:
            return "No tasks are currently running."
        
        if task_id:
            # Check specific task
            if task_id not in self.running_tasks:
                return f"Task {task_id} not found."
            
            task_info = self.running_tasks[task_id]
            status = "Completed" if task_info["completed"] else "Running"
            runtime = time.time() - task_info["start_time"]
            
            result = f"""Task Status: {task_id}
Description: {task_info["description"]}
Status: {status}
Runtime: {runtime:.2f} seconds
Output Buffer Size: {len(task_info["output_buffer"])} characters"""
            
            if task_info["completed"] and "end_time" in task_info:
                total_time = task_info["end_time"] - task_info["start_time"]
                result += f"\nTotal Time: {total_time:.2f} seconds"
            
            return result
        else:
            # Check all tasks
            result = f"Task Status Summary ({len(self.running_tasks)} tasks):\n"
            for tid, task_info in self.running_tasks.items():
                status = "Completed" if task_info["completed"] else "Running"
                runtime = time.time() - task_info["start_time"]
                result += f"\n{tid}: {task_info['description']} - {status} ({runtime:.1f}s)"
            
            return result
    
    def _task_results(self, args: Dict[str, Any]) -> str:
        """Retrieve the results and summaries from completed subagent tasks."""
        try:
            # Validate arguments - the error suggests args might not be a dict
            if not isinstance(args, dict):
                logger.error(f"_task_results received invalid args type: {type(args)}")
                return f"Error: Invalid arguments type: {type(args)}. Expected dictionary."
            
            include_running = args.get("include_running", False)
            clear_after_retrieval = args.get("clear_after_retrieval", True)
            
            # Check if running_tasks exists and is valid
            if not hasattr(self, 'running_tasks') or not isinstance(self.running_tasks, dict):
                return "No task manager found or tasks corrupted."
                
            if not self.running_tasks:
                return "No tasks found."
            
            # Simple approach - just return basic info to avoid complex iteration bugs
            task_count = len(self.running_tasks)
            completed_count = sum(1 for task in self.running_tasks.values() 
                                if isinstance(task, dict) and task.get("completed", False))
            running_count = task_count - completed_count
            
            result_parts = [
                f"=== TASK RESULTS SUMMARY ===",
                f"Total tasks: {task_count}",
                f"Completed: {completed_count}",
                f"Running: {running_count}",
                ""
            ]
            
            # Show completed tasks
            if completed_count > 0:
                result_parts.append("=== COMPLETED TASKS ===")
                for task_id, task_info in self.running_tasks.items():
                    if not isinstance(task_info, dict) or not task_info.get("completed", False):
                        continue
                    
                    runtime = task_info.get('end_time', time.time()) - task_info.get('start_time', 0)
                    result_parts.append(f"\n{task_id}: {task_info.get('description', 'Unknown')} - ✅ Completed ({runtime:.2f}s)")
                    
                    result = task_info.get("result", "No result")
                    if result and len(str(result)) > 10:
                        # Include full results (no truncation)
                        result_parts.append(f"Result: {str(result)}")
            
            # Show running tasks if requested
            if include_running and running_count > 0:
                result_parts.append("\n=== RUNNING TASKS ===")
                for task_id, task_info in self.running_tasks.items():
                    if not isinstance(task_info, dict) or task_info.get("completed", False):
                        continue
                    
                    runtime = time.time() - task_info.get('start_time', 0)
                    result_parts.append(f"\n{task_id}: {task_info.get('description', 'Unknown')} - ⏳ Running ({runtime:.2f}s)")
            
            # Clear completed tasks if requested
            if clear_after_retrieval and completed_count > 0:
                to_remove = [tid for tid, task in self.running_tasks.items() 
                           if isinstance(task, dict) and task.get("completed", False)]
                for task_id in to_remove:
                    del self.running_tasks[task_id]
                result_parts.append(f"\n--- {len(to_remove)} completed tasks cleared from memory ---")
            
            return "\n".join(result_parts)
        
        except Exception as e:
            logger.error(f"Error in _task_results: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return f"Error retrieving task results: {str(e)}"
    
    def _check_all_tasks_completed(self):
        """Check if all tasks in the current batch are completed and automatically report results."""
        if not self.running_tasks:
            return
        
        # Group tasks by batch
        batches = {}
        for task_id, task_info in self.running_tasks.items():
            batch_id = task_info.get("batch_id", "unknown")
            if batch_id not in batches:
                batches[batch_id] = []
            batches[batch_id].append((task_id, task_info))
        
        # Check each batch for completion
        for batch_id, batch_tasks in batches.items():
            completed_in_batch = [task for task_id, task in batch_tasks if task["completed"]]
            
            if (len(completed_in_batch) == len(batch_tasks) and 
                len(completed_in_batch) > 1 and 
                batch_id == self.current_batch_id):
                # All tasks in current batch are completed - generate consolidated summary
                print(f"\n\r🎉 [TASK MANAGER] All {len(completed_in_batch)} subagent tasks in {batch_id} have completed!\r")
                print(f"📋 [TASK MANAGER] Generating consolidated summary...\r")
                
                # Create consolidated summary for this batch
                summary = self._generate_consolidated_summary(batch_id)
                
                # Display the summary to the user
                print(f"\n\r📊 [SUBAGENT SUMMARY] Consolidated Results from {batch_id} ({len(completed_in_batch)} tasks):\r")
                print(f"{summary}\r")
                
                # Remove completed batch tasks
                for task_id, _ in batch_tasks:
                    if task_id in self.running_tasks:
                        del self.running_tasks[task_id]
    
    def _generate_consolidated_summary(self, batch_id: str = None) -> str:
        """Generate a consolidated summary of completed subagent tasks for a specific batch."""
        if not self.running_tasks:
            return "No completed tasks to summarize."
        
        # Filter tasks by batch if specified
        if batch_id:
            batch_tasks = {task_id: task_info for task_id, task_info in self.running_tasks.items() 
                          if task_info.get("batch_id") == batch_id and task_info["completed"]}
        else:
            batch_tasks = {task_id: task_info for task_id, task_info in self.running_tasks.items() 
                          if task_info["completed"]}
        
        if not batch_tasks:
            return "No completed tasks found for the specified batch."
        
        completed_tasks = list(batch_tasks.values())
        
        # Create structured summary
        summary_parts = [
            f"=== SUBAGENT TASK SUMMARY ({batch_id or 'All Tasks'}) ===",
            f"Completed Tasks: {len(completed_tasks)}",
        ]
        
        # Calculate total runtime with error handling
        try:
            # Extract valid timestamps and calculate runtime
            start_times = []
            end_times = []
            for task in completed_tasks:
                start_time = task.get('start_time')
                end_time = task.get('end_time', time.time())
                
                # Validate that times are numeric
                if isinstance(start_time, (int, float)) and isinstance(end_time, (int, float)):
                    start_times.append(start_time)
                    end_times.append(end_time)
            
            if start_times and end_times:
                total_runtime = max(end_times) - min(start_times)
                summary_parts.append(f"Total Runtime: {total_runtime:.2f} seconds")
            else:
                summary_parts.append("Total Runtime: Unable to calculate (invalid time data)")
        except Exception as e:
            logger.error(f"Error calculating total runtime: {e}")
            summary_parts.append("Total Runtime: Unable to calculate (calculation error)")
        
        summary_parts.extend([
            "",
            "=== INDIVIDUAL TASK RESULTS ==="
        ])
        
        for i, (task_id, task_info) in enumerate(sorted(batch_tasks.items()), 1):
            if not task_info["completed"]:
                continue
                
            # Calculate individual task runtime with error handling
            try:
                start_time = task_info.get('start_time')
                end_time = task_info.get('end_time', time.time())
                
                if isinstance(start_time, (int, float)) and isinstance(end_time, (int, float)):
                    runtime = end_time - start_time
                    runtime_str = f"{runtime:.2f} seconds"
                else:
                    runtime_str = "Unable to calculate (invalid time data)"
            except Exception as e:
                logger.error(f"Error calculating runtime for task {task_id}: {e}")
                runtime_str = "Unable to calculate (calculation error)"
            
            summary_parts.extend([
                f"\n--- Task {i}: {task_id} ---",
                f"Description: {task_info['description']}",
                f"Runtime: {runtime_str}",
                f"Status: {'✅ Completed' if task_info['completed'] else '⏳ Running'}",
                ""
            ])
            
            # Add the actual result from the subagent
            result = task_info.get("result", "No result available")
            if result and result != "No result available":
                # Include full results (no truncation)
                summary_parts.extend([
                    "Result:",
                    result,
                    ""
                ])
        
        summary_parts.extend([
            "=== SUMMARY COMPLETE ===",
            "",
            "All parallel subagent tasks have finished. The above results are now available",
            "for your analysis and next steps."
        ])
        
        return "\n".join(summary_parts)
    
    async def start_mcp_server(self, server_name: str, server_config) -> bool:
        """Start and connect to an MCP server using FastMCP."""
        try:
            logger.info(f"Starting MCP server: {server_name}")
            
            # Construct command and args for FastMCP client
            command = server_config.command[0]
            args = server_config.command[1:] + server_config.args
            
            # Create FastMCP client with stdio transport
            transport = StdioTransport(command=command, args=args, env=server_config.env)
            client = FastMCPClient(transport=transport)
            
            # Enter the context manager and store it for cleanup
            context_manager = client.__aenter__()
            await context_manager
            
            # Store the client and context manager
            self.mcp_clients[server_name] = client
            self._mcp_contexts = getattr(self, '_mcp_contexts', {})
            self._mcp_contexts[server_name] = client
            
            # Get available tools from this server
            tools_result = await client.list_tools()
            if tools_result and hasattr(tools_result, 'tools'):
                for tool in tools_result.tools:
                    tool_key = f"{server_name}:{tool.name}"
                    self.available_tools[tool_key] = {
                        "server": server_name,
                        "name": tool.name,
                        "description": tool.description,
                        "schema": tool.inputSchema if hasattr(tool, 'inputSchema') else {},
                        "client": client
                    }
                    logger.info(f"Registered tool: {tool_key}")
            elif hasattr(tools_result, '__len__'):
                # Handle list format
                for tool in tools_result:
                    tool_key = f"{server_name}:{tool.name}"
                    self.available_tools[tool_key] = {
                        "server": server_name,
                        "name": tool.name,
                        "description": tool.description,
                        "schema": tool.inputSchema if hasattr(tool, 'inputSchema') else {},
                        "client": client
                    }
                    logger.info(f"Registered tool: {tool_key}")
            
            logger.info(f"Successfully connected to MCP server: {server_name}")
            return True
            
        except Exception as e:
            import traceback
            logger.error(f"Failed to start MCP server {server_name}: {e}")
            logger.error(f"Full traceback: {traceback.format_exc()}")
            return False
    
    async def shutdown(self):
        """Shutdown all MCP connections."""
        logger.info("Shutting down MCP connections...")
        
        # Close FastMCP client sessions
        for server_name, client in self.mcp_clients.items():
            try:
                # Exit the context manager properly
                await client.__aexit__(None, None, None)
                logger.info(f"Closed client session for {server_name}")
            except Exception as e:
                logger.error(f"Error closing client session for {server_name}: {e}")
        
        self.mcp_clients.clear()
        self.available_tools.clear()
        if hasattr(self, '_mcp_contexts'):
            self._mcp_contexts.clear()
        
        # Shutdown subagent manager if present
        if hasattr(self, 'subagent_manager') and self.subagent_manager:
            await self.subagent_manager.terminate_all()
    
    # Centralized Subagent Management Methods
    def _on_subagent_message(self, message):
        """Callback for when a subagent message is received - display during yield period."""
        try:
            # Get task_id for identification (if available in message data)
            task_id = message.data.get('task_id', 'unknown') if hasattr(message, 'data') and message.data else 'unknown'
            
            if message.type == 'output':
                formatted = f"🤖 [SUBAGENT-{task_id}] {message.content}"
            elif message.type == 'status':
                formatted = f"📋 [SUBAGENT-{task_id}] {message.content}"
            elif message.type == 'error':
                formatted = f"❌ [SUBAGENT-{task_id}] {message.content}"
            elif message.type == 'result':
                formatted = f"✅ [SUBAGENT-{task_id}] Result: {message.content}"
            else:
                formatted = f"[SUBAGENT-{task_id} {message.type}] {message.content}"
            
            # Only display immediately if we're in yielding mode (subagents active)
            # This ensures clean separation between main agent and subagent output
            if self.subagent_manager and self.subagent_manager.get_active_count() > 0:
                # Subagents are active - display immediately during yield period
                self._display_subagent_message_immediately(formatted, message.type)
            else:
                # No active subagents - just log for now (main agent controls display)
                logger.info(f"Subagent message logged: {message.type} - {message.content[:50]}")
                
        except Exception as e:
            logger.error(f"Error handling subagent message: {e}")
    
    def _display_subagent_message_immediately(self, formatted: str, message_type: str):
        """Display subagent message immediately with proper terminal handling."""
        try:
            # Handle raw terminal mode properly
            import sys
            import termios
            import tty
            
            try:
                # Check if terminal is in raw mode
                stdin_fd = sys.stdin.fileno()
                current_attrs = termios.tcgetattr(stdin_fd)
                
                # Check if we're in raw mode (no echo, no canonical processing)
                in_raw_mode = not (current_attrs[3] & termios.ECHO) and not (current_attrs[3] & termios.ICANON)
                
                if in_raw_mode:
                    # Terminal is in raw mode - convert all \n to \r\n for proper display
                    formatted_with_crlf = formatted.replace('\n', '\r\n')
                    output = f"\r\n{formatted_with_crlf}\r\n"
                    sys.stdout.write(output)
                    sys.stdout.flush()
                    logger.info(f"Displayed subagent message in raw mode: {message_type}")
                else:
                    # Normal mode - use prompt_toolkit
                    try:
                        from prompt_toolkit.patch_stdout import patch_stdout
                        from prompt_toolkit import print_formatted_text
                        
                        with patch_stdout():
                            print_formatted_text(formatted)
                        logger.info(f"Displayed subagent message via patch_stdout: {message_type}")
                    except ImportError:
                        # Fallback to stderr
                        print(formatted, file=sys.stderr, flush=True)
                        logger.info(f"Displayed subagent message via stderr: {message_type}")
                        
            except (OSError, termios.error):
                # Terminal control not available - use stderr fallback
                print(formatted, file=sys.stderr, flush=True)
                logger.info(f"Displayed subagent message via stderr fallback: {message_type}")
                
        except Exception as e:
            logger.error(f"Error displaying subagent message immediately: {e}")
    
    async def _collect_subagent_results(self):
        """Wait for all subagents to complete and collect their results."""
        if not self.subagent_manager:
            return []
        
        import time
        results = []
        max_wait_time = 300  # 5 minutes max wait
        start_time = time.time()
        
        # Wait for all active subagents to complete
        while self.subagent_manager.get_active_count() > 0:
            if time.time() - start_time > max_wait_time:
                logger.error("Timeout waiting for subagents to complete")
                break
            await asyncio.sleep(0.5)
        
        # Collect results from completed subagents
        logger.info(f"Checking {len(self.subagent_manager.subagents)} subagents for results")
        for task_id, subagent in self.subagent_manager.subagents.items():
            logger.info(f"Subagent {task_id}: completed={subagent.completed}, has_result={subagent.result is not None}")
            if subagent.completed and subagent.result:
                results.append({
                    'task_id': task_id,
                    'description': subagent.description,
                    'content': subagent.result,
                    'runtime': time.time() - subagent.start_time
                })
                logger.info(f"Collected result from {task_id}: {len(subagent.result)} chars")
            elif subagent.completed:
                logger.warning(f"Subagent {task_id} completed but has no result stored")
            else:
                logger.info(f"Subagent {task_id} not yet completed")
        
        logger.info(f"Collected {len(results)} results from {len(self.subagent_manager.subagents)} subagents")
        return results

    async def _task(self, args: Dict[str, Any]) -> str:
        """Spawn a new subagent task."""
        if not self.subagent_manager:
            return "Error: Subagent management not available"
        
        description = args.get("description", "Investigation task")
        prompt = args.get("prompt", "")
        context = args.get("context", "")
        
        if not prompt:
            return "Error: prompt is required"
        
        # Add context to prompt if provided
        full_prompt = prompt
        if context:
            full_prompt += f"\n\nAdditional context: {context}"
        
        try:
            task_id = await self.subagent_manager.spawn_subagent(description, full_prompt)
            return f"Spawned subagent task: {task_id}\nDescription: {description}\nTask is running in the background - output will appear in the chat as it becomes available."
        except Exception as e:
            return f"Error spawning subagent: {e}"
    
    def _task_status(self, args: Dict[str, Any]) -> str:
        """Check status of subagent tasks."""
        if not self.subagent_manager:
            return "Error: Subagent management not available"
        
        import time
        active_count = self.subagent_manager.get_active_count()
        total_count = len(self.subagent_manager.subagents)
        completed_count = total_count - active_count
        
        if total_count == 0:
            return "No subagent tasks found. Use the 'task' tool to spawn subagents."
        
        status_lines = [f"Subagent Status: {active_count} active, {completed_count} completed, {total_count} total"]
        
        specific_task_id = args.get("task_id")
        if specific_task_id:
            if specific_task_id in self.subagent_manager.subagents:
                subagent = self.subagent_manager.subagents[specific_task_id]
                runtime = time.time() - subagent.start_time
                status = "completed" if subagent.completed else "running"
                result_info = f" | Result: {subagent.result[:100]}..." if subagent.completed and subagent.result else ""
                status_lines.append(f"Task {specific_task_id}: {status} (runtime: {runtime:.1f}s){result_info}")
            else:
                status_lines.append(f"Task {specific_task_id}: not found")
        else:
            # Show all tasks grouped by status
            active_tasks = []
            completed_tasks = []
            
            for task_id, subagent in self.subagent_manager.subagents.items():
                runtime = time.time() - subagent.start_time
                task_info = f"  {task_id}: {runtime:.1f}s - {subagent.description}"
                
                if subagent.completed:
                    completed_tasks.append(task_info + " ✅")
                else:
                    active_tasks.append(task_info + " 🔄")
            
            if active_tasks:
                status_lines.append("\nActive Tasks:")
                status_lines.extend(active_tasks)
            
            if completed_tasks:
                status_lines.append("\nCompleted Tasks:")
                status_lines.extend(completed_tasks)
        
        return "\n".join(status_lines)
    
    def _task_results(self, args: Dict[str, Any]) -> str:
        """Get results from completed tasks."""
        if not self.subagent_manager:
            return "Error: Subagent management not available"
        
        task_id = args.get("task_id")
        if not task_id:
            return "Error: task_id is required"
        
        if task_id not in self.subagent_manager.subagents:
            return f"Error: Task {task_id} not found"
        
        subagent = self.subagent_manager.subagents[task_id]
        if not subagent.completed:
            return f"Task {task_id} is still running"
        
        if subagent.result:
            return f"Task {task_id} result:\n{subagent.result}"
        else:
            return f"Task {task_id} completed but no result captured"

    async def _execute_mcp_tool(self, tool_key: str, arguments: Dict[str, Any]) -> str:
        """Execute an MCP tool (built-in or external) and return the result."""
        try:
            if tool_key not in self.available_tools:
                # Debug: show available tools when tool not found
                available_list = list(self.available_tools.keys())[:10]  # First 10 tools
                return f"Error: Tool {tool_key} not found. Available tools: {available_list}"
            
            tool_info = self.available_tools[tool_key]
            tool_name = tool_info["name"]
            
            # Forward to parent if this is a subagent (except for subagent management tools)
            import sys
            if self.is_subagent and self.comm_socket:
                excluded_tools = ['task', 'task_status', 'task_results']
                if tool_name not in excluded_tools:
                    # Tool forwarding happens silently
                    return await self._forward_tool_to_parent(tool_key, tool_name, arguments)
            elif self.is_subagent:
                sys.stderr.write(f"🤖 [SUBAGENT] WARNING: is_subagent=True but no comm_socket for tool {tool_name}\n")
                sys.stderr.flush()
            
            # Check if it's a built-in tool
            if tool_info["server"] == "builtin":
                logger.info(f"Executing built-in tool: {tool_name}")
                return await self._execute_builtin_tool(tool_name, arguments)
            
            # Handle external MCP tools with FastMCP
            client = tool_info["client"]
            if client is None:
                return f"Error: No client session for tool {tool_key}"
            
            logger.info(f"Executing MCP tool: {tool_name}")
            result = await client.call_tool(tool_name, arguments)
            
            # Format the result for FastMCP
            if hasattr(result, 'content') and result.content:
                content_parts = []
                for content in result.content:
                    if hasattr(content, 'text'):
                        content_parts.append(content.text)
                    elif hasattr(content, 'data'):
                        content_parts.append(str(content.data))
                    else:
                        content_parts.append(str(content))
                return "\n".join(content_parts)
            elif isinstance(result, str):
                return result
            elif isinstance(result, dict):
                return json.dumps(result, indent=2)
            else:
                return f"Tool executed successfully. Result type: {type(result)}, Content: {result}"
                
        except Exception as e:
            logger.error(f"Error executing tool {tool_key}: {e}")
            return f"Error executing tool {tool_key}: {str(e)}"

    async def _forward_tool_to_parent(self, tool_key: str, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Forward tool execution to parent agent via communication socket."""
        try:
            import json
            import uuid
            
            # Create unique request ID for tracking
            request_id = str(uuid.uuid4())
            
            # Prepare tool execution message
            message = {
                "type": "tool_execution_request",
                "request_id": request_id,
                "tool_key": tool_key,
                "tool_name": tool_name,
                "tool_args": arguments,
                "timestamp": time.time()
            }
            
            # Send request to parent (synchronous)
            message_json = json.dumps(message) + "\n"
            self.comm_socket.send(message_json.encode('utf-8'))
            
            # Wait for response with timeout
            response_timeout = 300.0  # 5 minutes timeout for tool execution
            self.comm_socket.settimeout(response_timeout)
            
            # Read response (synchronous)
            buffer = ""
            while True:
                try:
                    data = self.comm_socket.recv(4096).decode('utf-8')
                    if not data:
                        break
                    
                    buffer += data
                    
                    # Process complete messages (newline-delimited JSON)
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        if line.strip():
                            try:
                                response = json.loads(line.strip())
                                if (response.get("type") == "tool_execution_response" and 
                                    response.get("request_id") == request_id):
                                    
                                    # Return tool result
                                    if response.get("success", False):
                                        return response.get("result", "Tool executed successfully")
                                    else:
                                        error = response.get("error", "Unknown error")
                                        return f"Error from parent: {error}"
                                        
                            except json.JSONDecodeError:
                                continue
                                
                except Exception as e:
                    logger.error(f"Error receiving response from parent: {e}")
                    break
            
            return f"Error: No response received from parent for tool {tool_name}"
            
        except Exception as e:
            logger.error(f"Error forwarding tool {tool_name} to parent: {e}")
            return f"Error forwarding tool to parent: {str(e)}"

    async def _execute_mcp_tool_with_keepalive(self, tool_key: str, arguments: Dict[str, Any], input_handler=None, keepalive_interval: float = 5.0) -> tuple:
        """Execute an MCP tool with keep-alive messages, returning (result, keepalive_messages)."""
        import asyncio
        
        # Create the tool execution task
        tool_task = asyncio.create_task(self._execute_mcp_tool(tool_key, arguments))
        
        # Keep-alive configuration
        keepalive_messages = []
        start_time = asyncio.get_event_loop().time()
        
        # Monitor the task and collect keep-alive messages
        while not tool_task.done():
            try:
                # Check for interruption before waiting
                if input_handler and input_handler.interrupted:
                    tool_task.cancel()
                    keepalive_messages.append("🛑 Tool execution cancelled by user")
                    try:
                        await tool_task
                    except asyncio.CancelledError:
                        pass
                    return "Tool execution cancelled", keepalive_messages
                
                # Wait for either task completion or timeout
                await asyncio.wait_for(asyncio.shield(tool_task), timeout=keepalive_interval)
                break  # Task completed
            except asyncio.TimeoutError:
                # Task is still running, send keep-alive message
                current_time = asyncio.get_event_loop().time()
                elapsed = current_time - start_time
                
                # Create a keep-alive message
                keepalive_msg = f"⏳ Tool {tool_key} still running... ({elapsed:.1f}s elapsed)"
                if input_handler:
                    keepalive_msg += ", press ESC to cancel"
                keepalive_messages.append(keepalive_msg)
                logger.debug(f"Keep-alive: {keepalive_msg}")
                continue
        
        # Get the final result
        result = await tool_task
        return result, keepalive_messages
    
    def _create_system_prompt(self, for_first_message: bool = False) -> str:
        """Create a basic system prompt that includes tool information."""
        tool_descriptions = []
        
        for tool_key, tool_info in self.available_tools.items():
            # Use the converted name format (with underscores)
            converted_tool_name = tool_key.replace(":", "_")
            description = tool_info["description"]
            tool_descriptions.append(f"- **{converted_tool_name}**: {description}")
        
        tools_text = "\n".join(tool_descriptions) if tool_descriptions else "No tools available"
        
        # Basic system prompt - subclasses can override this
        system_prompt = f"""You are a top-tier autonomous software development agent. You are in control and responsible for completing the user's request.

**Mission:** Use the available tools to solve the user's request.

**Guiding Principles:**
- **Ponder, then proceed:** Briefly outline your plan before you act. State your assumptions.
- **Bias for action:** You are empowered to take initiative. Do not ask for permission, just do the work.
- **Problem-solve:** If a tool fails, analyze the error and try a different approach.
- **Break large changes into smaller chunks:** For large code changes, divide the work into smaller, manageable tasks to ensure clarity and reduce errors.

**File Reading Strategy:**
- **Be surgical:** Do not read entire files at once. It is a waste of your context window.
- **Locate, then read:** Use tools like `grep` or `find` to locate the specific line numbers or functions you need to inspect.
- **Read in chunks:** Read files in smaller, targeted chunks of 50-100 lines using the `offset` and `limit` parameters in the `read_file` tool.
- **Full reads as a last resort:** Only read a full file if you have no other way to find what you are looking for.

**File Editing Workflow:**
1.  **Read first:** Always read a file before you try to edit it, following the file reading strategy above.
2.  **Greedy Grepping:** Always `grep` or look for a small section around where you want to do an edit. This is faster and more reliable than reading the whole file.
3.  **Use `replace_in_file`:** For all file changes, use `builtin_replace_in_file` to replace text in files.
4.  **Chunk changes:** Break large edits into smaller, incremental changes to maintain control and clarity.

**Todo List Workflow:**
- **Use the Todo list:** Use `builtin_todo_read` and `builtin_todo_write` to manage your tasks.
- **Start with a plan:** At the beginning of your session, create a todo list to outline your steps.
- **Update as you go:** As you complete tasks, update the todo list to reflect your progress.

**Subagent Workflow:**
- **Parallel execution:** For complex investigations requiring multiple independent tasks, spawn multiple subagents simultaneously by making multiple `builtin_task` calls in the same response.
- **Automatic coordination:** After spawning subagents, the main agent automatically pauses, waits for all subagents to complete, then restarts with their combined results.
- **Do not poll status:** Avoid calling `builtin_task_status` repeatedly - the system handles coordination automatically.
- **Single response spawning:** To spawn multiple subagents, include all `builtin_task` calls in one response, not across multiple responses.

**Workflow:**
1.  **Reason:** Outline your plan.
2.  **Act:** Use one or more tool calls to execute your plan. Use parallel tool calls when it makes sense.
3.  **Respond:** When you have completed the request, provide the final answer to the user.

**Available Tools:**
{tools_text}

You are the expert. Complete the task."""

        return system_prompt
    

    def format_markdown(self, text: str) -> str:
        """Format markdown text for terminal display."""
        if not text:
            return text
            
        # Simple terminal-friendly markdown formatting
        lines = text.split('\n')
        formatted_lines = []
        
        for line in lines:
            # Headers
            if line.startswith('# '):
                formatted_lines.append(f"\n\033[1m\033[4m{line[2:]}\033[0m")  # Bold + underline
            elif line.startswith('## '):
                formatted_lines.append(f"\n\033[1m{line[3:]}\033[0m")  # Bold
            elif line.startswith('### '):
                formatted_lines.append(f"\n\033[1m{line[4:]}\033[0m")  # Bold
            
            # Code blocks
            elif line.strip().startswith('```'):
                if line.strip() == '```':
                    formatted_lines.append("\033[2m" + line + "\033[0m")  # Dim
                else:
                    formatted_lines.append("\033[2m" + line + "\033[0m")  # Dim
            
            # Lists
            elif re.match(r'^\s*[-*+]\s', line):
                formatted_lines.append(f"\033[36m•\033[0m{line[line.index(' ', line.index('-') if '-' in line else line.index('*') if '*' in line else line.index('+')):]}")
            elif re.match(r'^\s*\d+\.\s', line):
                formatted_lines.append(f"\033[36m{line.split('.')[0]}.\033[0m{line[line.index('.') + 1:]}")
            
            # Regular line - process inline formatting
            else:
                # Bold
                line = re.sub(r'\*\*(.*?)\*\*', r'\033[1m\1\033[0m', line)
                # Italic (using dim since true italic isn't widely supported)
                line = re.sub(r'\*(.*?)\*', r'\033[3m\1\033[0m', line)
                # Inline code
                line = re.sub(r'`(.*?)`', r'\033[47m\033[30m\1\033[0m', line)
                
                formatted_lines.append(line)
        
        return '\n'.join(formatted_lines)
    
    def display_tool_execution_start(self, tool_count: int, is_subagent: bool = False, interactive: bool = True) -> str:
        """Display tool execution start message."""
        if is_subagent:
            return f"🤖 [SUBAGENT] Executing {tool_count} tool(s)..."
        else:
            return f"🔧 Using {tool_count} tool(s)..."
    
    def display_tool_execution_step(self, step_num: int, tool_name: str, arguments: dict, is_subagent: bool = False, interactive: bool = True) -> str:
        """Display individual tool execution step."""
        if is_subagent:
            return f"🤖 [SUBAGENT] Step {step_num}: Executing {tool_name}..."
        else:
            return f"{step_num}. Executing {tool_name}..."
    
    def display_tool_execution_result(self, result: str, is_error: bool = False, is_subagent: bool = False, interactive: bool = True) -> str:
        """Display tool execution result."""
        if is_error:
            prefix = "❌ [SUBAGENT] Error:" if is_subagent else "❌ Error:"
        else:
            prefix = "✅ [SUBAGENT] Result:" if is_subagent else "✅ Result:"
        
        # Truncate long results for display
        if len(result) > 200:
            result_preview = result[:200] + "..."
        else:
            result_preview = result
            
        return f"{prefix} {result_preview}"
    
    def display_tool_processing(self, is_subagent: bool = False, interactive: bool = True) -> str:
        """Display tool processing message."""
        if is_subagent:
            return "🤖 [SUBAGENT] Processing tool results..."
        else:
            return "⚙️ Processing tool results..."
    
    def estimate_tokens(self, text: str) -> int:
        """Rough estimation of tokens (1 token ≈ 4 characters for most models)."""
        return len(text) // 4
    
    def count_conversation_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """Count estimated tokens in the conversation."""
        total_tokens = 0
        for message in messages:
            if isinstance(message.get('content'), str):
                total_tokens += self.estimate_tokens(message['content'])
            # Add small overhead for role and structure
            total_tokens += 10
        return total_tokens
    
    def get_token_limit(self) -> int:
        """Get the context token limit for the current model."""
        # Enhanced centralized token limit management with model configuration support
        model_limits = self._get_model_token_limits()
        
        # Try to get model name from config
        model_name = self._get_current_model_name()
        
        if model_name and model_name in model_limits:
            return model_limits[model_name]
        
        # Fallback: check for model patterns
        if model_name:
            for pattern, limit in model_limits.items():
                if pattern in model_name.lower():
                    return limit
        
        # Conservative default - subclasses can override
        return 32000
    
    def _get_model_token_limits(self) -> Dict[str, int]:
        """Define token limits for known models. Subclasses can extend this."""
        return {
            # DeepSeek models
            "deepseek-reasoner": 128000,
            "deepseek-chat": 64000,
            
            # Gemini models  
            "gemini-pro": 128000,
            "pro": 128000,  # Pattern matching for any "pro" model
            "gemini-flash": 64000,
            "flash": 64000,  # Pattern matching for any "flash" model
            
            # Common defaults
            "gpt-4": 128000,
            "gpt-3.5": 16000,
            "claude": 200000,
        }
    
    def _get_current_model_name(self) -> Optional[str]:
        """Get the current model name. Subclasses should override to provide specific model."""
        # Try common config patterns
        for attr_name in ['deepseek_config', 'gemini_config', 'openai_config']:
            if hasattr(self, attr_name):
                config = getattr(self, attr_name)
                if hasattr(config, 'model'):
                    return config.model
        
        return None
    
    def should_compact(self, messages: List[Dict[str, Any]]) -> bool:
        """Determine if conversation should be compacted."""
        current_tokens = self.count_conversation_tokens(messages)
        limit = self.get_token_limit()
        # Compact when we're at 80% of the limit
        return current_tokens > (limit * 0.8)
    
    async def compact_conversation(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Create a compact summary of the conversation to preserve context while reducing tokens."""
        if len(messages) <= 3:  # Keep conversations that are already short
            return messages
        
        # Always keep the first message (system prompt) and last 2 messages
        system_message = messages[0] if messages[0].get('role') == 'system' else None
        recent_messages = messages[-2:]
        
        # Messages to summarize (everything except system and last 2)
        start_idx = 1 if system_message else 0
        messages_to_summarize = messages[start_idx:-2]
        
        if not messages_to_summarize:
            return messages
        
        # Create summary prompt
        conversation_text = "\n".join([
            f"{msg.get('role', 'unknown')}: {msg.get('content', '')}"
            for msg in messages_to_summarize
        ])
        
        summary_prompt = f"""Please create a concise summary of this conversation that preserves:
1. Key decisions and actions taken
2. Important file changes or tool usage
3. Current project state and context
4. Any pending tasks or next steps

Conversation to summarize:
{conversation_text}

Provide a brief but comprehensive summary that maintains continuity for ongoing work."""

        try:
            # Use the current model to create summary
            summary_messages = [{"role": "user", "content": summary_prompt}]
            summary_response = await self.generate_response(summary_messages, tools=None)
            
            # Create condensed conversation
            condensed = []
            if system_message:
                condensed.append(system_message)
            
            # Add summary as a system message
            condensed.append({
                "role": "system", 
                "content": f"[CONVERSATION SUMMARY] {summary_response}"
            })
            
            # Add recent messages
            condensed.extend(recent_messages)
            
            print(f"\n🗜️  Conversation compacted: {len(messages)} → {len(condensed)} messages")
            return condensed
            
        except Exception as e:
            print(f"⚠️  Failed to compact conversation: {e}")
            # Fallback: just keep system + last 5 messages
            fallback = []
            if system_message:
                fallback.append(system_message)
            fallback.extend(messages[-5:])
            return fallback
    
    async def generate_response(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict]] = None) -> Union[str, Any]:
        """Generate a response using the specific LLM. Centralized implementation."""
        # For subagents, use interactive=False to avoid terminal formatting issues
        interactive = not self.is_subagent
        
        # Default to streaming behavior, but allow subclasses to override
        stream = getattr(self, 'stream', True)
        
        # Call the concrete implementation's chat_completion method
        return await self.chat_completion(messages, stream=stream, interactive=interactive)
    
    # Tool conversion and parsing helper methods
    def normalize_tool_name(self, tool_key: str) -> str:
        """Normalize tool name by replacing colons with underscores."""
        return tool_key.replace(":", "_")
    
    def generate_default_description(self, tool_info: dict) -> str:
        """Generate a default description for a tool if none exists."""
        return tool_info.get("description") or f"Execute {tool_info['name']} tool"
    
    def get_tool_schema(self, tool_info: dict) -> dict:
        """Get tool schema with fallback to basic object schema."""
        return tool_info.get("schema") or {"type": "object", "properties": {}}
    
    def validate_json_arguments(self, args_json: str) -> bool:
        """Validate that a string contains valid JSON."""
        try:
            json.loads(args_json)
            return True
        except (json.JSONDecodeError, TypeError):
            return False
    
    def validate_tool_name(self, tool_name: str) -> bool:
        """Validate tool name format."""
        return tool_name and (tool_name.startswith("builtin_") or "_" in tool_name)
    
    def create_tool_call_object(self, name: str, args: str, call_id: str = None):
        """Create a standardized tool call object."""
        import types
        
        # Create a SimpleNamespace object similar to OpenAI's format
        tool_call = types.SimpleNamespace()
        tool_call.function = types.SimpleNamespace()
        tool_call.function.name = name
        tool_call.function.arguments = args
        tool_call.id = call_id or f"call_{name}_{int(time.time())}"
        tool_call.type = "function"
        
        return tool_call
    
    @abstractmethod
    def convert_tools_to_llm_format(self) -> List[Dict]:
        """Convert tools to the specific LLM's format. Must be implemented by subclasses."""
        pass
    
    @abstractmethod
    def parse_tool_calls(self, response: Any) -> List[Dict[str, Any]]:
        """Parse tool calls from the LLM response. Must be implemented by subclasses."""
        pass
    
    async def interactive_chat(self, input_handler: InterruptibleInput, existing_messages: List[Dict[str, Any]] = None):
        """Interactive chat session with shared functionality."""
        messages = existing_messages or []
        current_task = None
        
        print("Starting interactive chat. Type /quit or /exit to end, /tools to list available tools.")
        print("Use /help for slash commands. Press ESC at any time to interrupt operations.\n")
        
        while True:
            try:
                # Cancel any pending task if interrupted
                if input_handler.interrupted and current_task and not current_task.done():
                    current_task.cancel()
                    try:
                        await current_task
                    except asyncio.CancelledError:
                        pass
                    input_handler.interrupted = False
                    current_task = None
                    continue
                
                # Get user input with smart multiline detection
                user_input = input_handler.get_multiline_input("You: ")
                
                if user_input is None:  # Interrupted
                    if current_task and not current_task.done():
                        current_task.cancel()
                        print("🛑 Operation cancelled by user")
                    input_handler.interrupted = False
                    current_task = None
                    continue
                
                # Handle slash commands
                if user_input.strip().startswith('/'):
                    try:
                        slash_response = await self.slash_commands.handle_slash_command(user_input.strip(), messages)
                        if slash_response:
                            # Handle special command responses
                            if isinstance(slash_response, dict):
                                if "compacted_messages" in slash_response:
                                    print(f"\n{slash_response['status']}\n")
                                    messages[:] = slash_response["compacted_messages"]  # Update messages in place
                                elif "clear_messages" in slash_response:
                                    print(f"\n{slash_response['status']}\n")
                                    messages.clear()  # Clear the local messages list
                                elif "quit" in slash_response:
                                    print(f"\n{slash_response['status']}")
                                    break  # Exit the chat loop
                                elif "reload_host" in slash_response:
                                    print(f"\n{slash_response['status']}")
                                    return {"reload_host": slash_response["reload_host"], "messages": messages}
                                else:
                                    print(f"\n{slash_response.get('status', str(slash_response))}\n")
                            else:
                                print(f"\n{slash_response}\n")
                            continue
                    except Exception as e:
                        print(f"\nError handling slash command: {e}\n")
                        continue
                
                if not user_input.strip():
                    # Empty input, just continue
                    continue
                
                # Add user message
                messages.append({"role": "user", "content": user_input})
                
                # Show thinking message
                print("\nThinking...")
                
                # Create response task
                tools_list = self.convert_tools_to_llm_format()
                current_task = asyncio.create_task(
                    self.generate_response(messages, tools_list)
                )
                
                # Wait for response with simple interruption handling
                try:
                    await current_task
                except asyncio.CancelledError:
                    print("\n🛑 Request cancelled")
                    input_handler.interrupted = False
                    current_task = None
                    continue
                except Exception as e:
                    print(f"\nError generating response: {e}")
                    current_task = None
                    continue
                
                # Get the response
                response = current_task.result()
                current_task = None
                
                if hasattr(response, '__aiter__'):
                    # Streaming response
                    print("\nAssistant (press ESC to interrupt):")
                    sys.stdout.flush()
                    full_response = ""
                    
                    # Set up non-blocking input monitoring
                    stdin_fd = sys.stdin.fileno()
                    old_settings = termios.tcgetattr(stdin_fd)
                    tty.setraw(stdin_fd)
                    
                    interrupted = False
                    try:
                        async for chunk in response:
                            # Check for escape key on each chunk
                            if select.select([sys.stdin], [], [], 0)[0]:  # Non-blocking check
                                char = sys.stdin.read(1)
                                if char == '\x1b':  # Escape key
                                    interrupted = True
                                    break
                            
                            # Check for interruption flag
                            if input_handler.interrupted:
                                interrupted = True
                                input_handler.interrupted = False
                                break
                                
                            if isinstance(chunk, str):
                                # Convert \n to \r\n for proper terminal display in raw mode
                                display_chunk = chunk.replace('\n', '\r\n')
                                print(display_chunk, end="", flush=True)
                                full_response += chunk
                            else:
                                # Handle any non-string chunks if needed
                                display_chunk = str(chunk).replace('\n', '\r\n')
                                print(display_chunk, end="", flush=True)
                                full_response += str(chunk)
                    finally:
                        # Always restore terminal settings first
                        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)
                        
                        # Clean up display if interrupted
                        if interrupted:
                            print("\n🛑 Streaming interrupted by user")
                            sys.stdout.flush()
                        else:
                            print()  # Normal newline after streaming
                    
                    # Add assistant response to messages
                    if full_response:  # Only add if not interrupted
                        messages.append({"role": "assistant", "content": full_response})
                else:
                    # Non-streaming response
                    print(f"\nAssistant: {response}")
                    messages.append({"role": "assistant", "content": str(response)})
                
            except KeyboardInterrupt:
                # Move to beginning of line and clear, then print exit message
                sys.stdout.write('\r\x1b[KExiting...\n')
                sys.stdout.flush()
                break
            except Exception as e:
                print(f"\nError: {e}")



# CLI functionality
@click.group()
@click.option('--config-file', default=None, help='Path to the configuration file (default: ~/.mcp/config.json)')
@click.pass_context
def cli(ctx, config_file):
    """MCP Agent - Run AI models with MCP tool integration."""
    ctx.ensure_object(dict)
    ctx.obj['config_file'] = config_file


@cli.command()
def init():
    """Initialize configuration file."""
    from config import create_sample_env
    create_sample_env()


@cli.command('switch-chat')
@click.pass_context
def switch_chat(ctx):
    """Switch the model to deepseek-chat."""
    config = load_config()
    config.deepseek_model = "deepseek-chat"
    click.echo(f"Model switched to: {config.deepseek_model}")
    # Save the updated config
    config.save()


@cli.command('switch-reason')
@click.pass_context
def switch_reason(ctx):
    """Switch the model to deepseek-reasoner."""
    config = load_config()
    config.deepseek_model = "deepseek-reasoner"
    click.echo(f"Model switched to: {config.deepseek_model}")
    # Save the updated config
    config.save()


@cli.command('switch-gemini')
@click.pass_context
def switch_gemini(ctx):
    """Switch to use Gemini Flash 2.5 as the backend model."""
    config = load_config()
    # Set Gemini Flash as the model and switch backend
    config.deepseek_model = "gemini"  # Use this as a marker
    config.gemini_model = "gemini-2.5-flash"
    click.echo(f"Backend switched to: Gemini Flash 2.5 ({config.gemini_model})")
    # Save the updated config
    config.save()


@cli.command('switch-gemini-pro')
@click.pass_context
def switch_gemini_pro(ctx):
    """Switch to use Gemini Pro 2.5 as the backend model."""
    config = load_config()
    # Set Gemini Pro as the model and switch backend
    config.deepseek_model = "gemini"  # Use this as a marker
    config.gemini_model = "gemini-2.5-pro"
    click.echo(f"Backend switched to: Gemini Pro 2.5 ({config.gemini_model})")
    # Save the updated config
    config.save()


@cli.command()
@click.option('--server', multiple=True, help='MCP server to connect to (format: name:command:arg1:arg2)')
@click.pass_context
async def chat(ctx, server):
    """Start interactive chat session."""
    try:
        # Load configuration
        config = load_config()
        
        # Load configuration and create host directly
        
        # Check if Gemini backend should be used
        if config.deepseek_model == "gemini":
            if not config.gemini_api_key:
                click.echo("Error: GEMINI_API_KEY not set. Run 'init' command first and update .env file.")
                return
            
            # Import and create Gemini host
            from mcp_gemini_host import MCPGeminiHost
            host = MCPGeminiHost(config)
            click.echo(f"Using model: {config.gemini_model}")
            click.echo(f"Temperature: {config.gemini_temperature}")
        else:
            if not config.deepseek_api_key:
                click.echo("Error: DEEPSEEK_API_KEY not set. Run 'init' command first and update .env file.")
                return
            
            # Create Deepseek host with new subagent system
            from mcp_deepseek_host import MCPDeepseekHost
            host = MCPDeepseekHost(config)
            click.echo(f"Using model: {config.deepseek_model}")
            click.echo(f"Temperature: {config.deepseek_temperature}")
        
        # Connect to additional MCP servers specified via --server option
        for server_spec in server:
            parts = server_spec.split(':')
            if len(parts) < 2:
                click.echo(f"Invalid server spec: {server_spec}")
                continue
            
            server_name = parts[0]
            command = parts[1:]
            
            config.add_mcp_server(server_name, command)
        
        # Connect to all configured MCP servers (persistent + command-line)
        for server_name, server_config in config.mcp_servers.items():
            click.echo(f"Starting MCP server: {server_name}")
            success = await host.start_mcp_server(server_name, server_config)
            if not success:
                click.echo(f"Failed to start server: {server_name}")
            else:
                click.echo(f"✅ Connected to MCP server: {server_name}")
        
        # Start interactive chat with host reloading support
        input_handler = InterruptibleInput()
        messages = []
        
        while True:
            chat_result = await host.interactive_chat(input_handler, messages)
            
            # Check if we need to reload the host
            if isinstance(chat_result, dict) and "reload_host" in chat_result:
                messages = chat_result.get("messages", [])
                reload_type = chat_result["reload_host"]
                
                # Shutdown current host
                await host.shutdown()
                
                # Reload config and create new host
                config = load_config()
                
                if reload_type == "gemini":
                    if not config.gemini_api_key:
                        click.echo("Error: GEMINI_API_KEY not set. Cannot switch to Gemini.")
                        break
                    from mcp_gemini_host import MCPGeminiHost
                    host = MCPGeminiHost(config)
                    click.echo(f"Switched to model: {config.gemini_model}")
                    click.echo(f"Temperature: {config.gemini_temperature}")
                else:  # deepseek
                    if not config.deepseek_api_key:
                        click.echo("Error: DEEPSEEK_API_KEY not set. Cannot switch to DeepSeek.")
                        break
                    from mcp_deepseek_host import MCPDeepseekHost
                    host = MCPDeepseekHost(config)
                    click.echo(f"Switched to model: {config.deepseek_model}")
                    click.echo(f"Temperature: {config.deepseek_temperature}")
                
                # Reconnect to MCP servers
                for server_name, server_config in config.mcp_servers.items():
                    click.echo(f"Reconnecting to MCP server: {server_name}")
                    success = await host.start_mcp_server(server_name, server_config)
                    if success:
                        click.echo(f"✅ Reconnected to MCP server: {server_name}")
                    else:
                        click.echo(f"⚠️  Failed to reconnect to MCP server: {server_name}")
                
                # Continue with the same input handler and preserved messages
                print(f"\n🔄 Continuing chat with {len(messages)} preserved messages...\n")
                continue
            else:
                # Normal exit from chat
                break
        
    except KeyboardInterrupt:
        pass
    finally:
        if 'host' in locals():
            if hasattr(host.shutdown, '__call__') and asyncio.iscoroutinefunction(host.shutdown):
                await host.shutdown()
            else:
                host.shutdown()


@cli.command()
@click.argument('message')
@click.option('--server', multiple=True, help='MCP server to connect to')
@click.pass_context
async def ask(ctx, message, server):
    """Ask a single question."""
    try:
        config = load_config()
        
        # Check if Gemini backend should be used
        if config.deepseek_model == "gemini":
            if not config.gemini_api_key:
                click.echo("Error: GEMINI_API_KEY not set. Run 'init' command first and update .env file.")
                return
            
            # Import and create Gemini host
            from mcp_gemini_host import MCPGeminiHost
            host = MCPGeminiHost(config)
        else:
            if not config.deepseek_api_key:
                click.echo("Error: DEEPSEEK_API_KEY not set. Run 'init' command first and update .env file.")
                return
            
            from mcp_deepseek_host import MCPDeepseekHost
            host = MCPDeepseekHost(config)
        
        # Connect to servers
        for server_spec in server:
            parts = server_spec.split(':')
            if len(parts) < 2:
                continue
            
            server_name = parts[0]
            command = parts[1:]
            config.add_mcp_server(server_name, command)
            success = await host.start_mcp_server(server_name, config.mcp_servers[server_name])
            if not success:
                click.echo(f"Warning: Failed to connect to MCP server '{server_name}', continuing without it...")
        
        # Get response
        messages = [{"role": "user", "content": message}]
        response = await host.chat_completion(messages, stream=False)
        
        click.echo(response)
        
    finally:
        if 'host' in locals():
            if hasattr(host.shutdown, '__call__') and asyncio.iscoroutinefunction(host.shutdown):
                await host.shutdown()
            else:
                host.shutdown()


@cli.command()
@click.pass_context
async def compact(ctx):
    """Show conversation token usage and compacting options."""
    click.echo("Compact functionality is only available in interactive chat mode.")
    click.echo("Use 'python agent.py chat' and then '/tokens' or '/compact' commands.")


@cli.command('execute-task')
@click.argument('task_file_path')
def execute_task_command(task_file_path):
    """Execute a task from a task file (used for subprocess execution)."""
    import asyncio
    asyncio.run(execute_task_subprocess(task_file_path))


async def execute_task_subprocess(task_file_path: str):
    """Execute a task from a JSON file in subprocess mode."""
    try:
        import json
        import os
        import time
        
        # Load task data from file
        if not os.path.exists(task_file_path):
            print(f"Error: Task file not found: {task_file_path}")
            return
        
        with open(task_file_path, 'r') as f:
            task_data = json.load(f)
        
        task_id = task_data.get("task_id", "unknown")
        description = task_data.get("description", "")
        task_prompt = task_data.get("prompt", "")
        comm_port = task_data.get("comm_port")
        
        print(f"🤖 [SUBAGENT {task_id}] Starting task: {description}")
        
        # Connect to parent for tool execution forwarding
        comm_socket = None
        if comm_port:
            try:
                import socket
                comm_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                comm_socket.connect(('localhost', comm_port))
                print(f"🤖 [SUBAGENT {task_id}] Connected to parent for tool forwarding")
            except Exception as e:
                print(f"🤖 [SUBAGENT {task_id}] Warning: Could not connect to parent: {e}")
                comm_socket = None
        
        # Load configuration
        config = load_config()
        
        # Create appropriate host instance with subagent flag and communication socket
        if hasattr(config, 'deepseek_model') and config.deepseek_model == "gemini":
            from mcp_gemini_host import MCPGeminiHost
            subagent = MCPGeminiHost(config, is_subagent=True)
            print(f"🤖 [SUBAGENT {task_id}] Created Gemini subagent with is_subagent=True")
        else:
            from mcp_deepseek_host import MCPDeepseekHost
            subagent = MCPDeepseekHost(config, is_subagent=True)
            print(f"🤖 [SUBAGENT {task_id}] Created DeepSeek subagent with is_subagent=True")
        
        # Set communication socket for tool forwarding
        if comm_socket:
            subagent.comm_socket = comm_socket
            print(f"🤖 [SUBAGENT {task_id}] Communication socket configured for tool forwarding")
        else:
            print(f"🤖 [SUBAGENT {task_id}] WARNING: No communication socket - tools will execute locally")
        
        # Connect to MCP servers
        for server_name, server_config in config.mcp_servers.items():
            try:
                await subagent.start_mcp_server(server_name, server_config)
            except Exception as e:
                print(f"🤖 [SUBAGENT {task_id}] Warning: Failed to connect to MCP server {server_name}: {e}")
        
        # Execute the task
        messages = [{"role": "user", "content": task_prompt}]
        tools_list = subagent.convert_tools_to_llm_format()
        
        print(f"🤖 [SUBAGENT {task_id}] Executing task with {len(tools_list)} tools available...")
        
        # Get response from subagent
        response = await subagent.generate_response(messages, tools_list)
        
        # Handle streaming response
        if hasattr(response, '__aiter__'):
            full_response = ""
            async for chunk in response:
                if isinstance(chunk, str):
                    print(chunk, end='', flush=True)
                    full_response += chunk
            response = full_response
        else:
            print(response)
        
        # Clean up connections
        await subagent.shutdown()
        
        # Extract the final response for summary
        final_response = response if isinstance(response, str) else str(response)
        
        # Write result to a result file for the parent to collect
        result_file_path = task_file_path.replace('.json', '_result.json')
        result_data = {
            "task_id": task_id,
            "description": description,
            "status": "completed",
            "result": final_response,
            "timestamp": time.time()
        }
        
        with open(result_file_path, 'w') as f:
            json.dump(result_data, f, indent=2)
        
        print(f"\n🤖 [SUBAGENT {task_id}] Task completed successfully")
        
    except Exception as e:
        print(f"🤖 [SUBAGENT ERROR] Failed to execute task: {e}")
        import traceback
        traceback.print_exc()


@cli.group()
def mcp():
    """Manage MCP servers."""
    pass


@mcp.command()
@click.argument('server_spec')
@click.option('--env', multiple=True, help='Environment variable (format: KEY=VALUE)')
def add(server_spec, env):
    """Add a persistent MCP server configuration.
    
    Format: name:command:arg1:arg2:...
    
    Examples:
        python agent.py mcp add digitalocean:node:/path/to/digitalocean-mcp/dist/index.js
        python agent.py mcp add filesystem:python:-m:mcp.server.stdio:filesystem:--root:.
    """
    try:
        config = load_config()
        
        # Parse server specification
        parts = server_spec.split(':')
        if len(parts) < 2:
            click.echo("❌ Invalid server specification. Format: name:command:arg1:arg2:...")
            return
        
        name = parts[0]
        command = parts[1]
        args = parts[2:] if len(parts) > 2 else []
        
        # Parse environment variables
        env_dict = {}
        for env_var in env:
            if '=' in env_var:
                key, value = env_var.split('=', 1)
                env_dict[key] = value
            else:
                click.echo(f"Warning: Invalid environment variable format: {env_var}")
        
        # Add the server
        config.add_mcp_server(name, [command], args, env_dict)
        config.save_mcp_servers()
        
        click.echo(f"✅ Added MCP server '{name}'")
        click.echo(f"   Command: {command} {' '.join(args)}")
        if env_dict:
            click.echo(f"   Environment: {env_dict}")
        
    except Exception as e:
        click.echo(f"❌ Error adding MCP server: {e}")


@mcp.command()
def list():
    """List all configured MCP servers."""
    try:
        config = load_config()
        
        if not config.mcp_servers:
            click.echo("No MCP servers configured.")
            click.echo("Add a server with: python agent.py mcp add <name:command:args...>")
            return
        
        click.echo("Configured MCP servers:")
        click.echo()
        
        for name, server_config in config.mcp_servers.items():
            click.echo(f"📡 {name}")
            click.echo(f"   Command: {' '.join(server_config.command + server_config.args)}")
            if server_config.env:
                click.echo(f"   Environment: {server_config.env}")
            click.echo()
            
    except Exception as e:
        click.echo(f"❌ Error listing MCP servers: {e}")


@mcp.command()
@click.argument('name')
def remove(name):
    """Remove a persistent MCP server configuration."""
    try:
        config = load_config()
        
        if config.remove_mcp_server(name):
            config.save_mcp_servers()
            click.echo(f"✅ Removed MCP server '{name}'")
        else:
            click.echo(f"❌ MCP server '{name}' not found")
            
    except Exception as e:
        click.echo(f"❌ Error removing MCP server: {e}")


def main():
    """Main entry point."""
    # Store original async callbacks
    original_chat = chat.callback
    original_ask = ask.callback
    original_compact = compact.callback
    
    # Convert async commands to sync
    def sync_chat(**kwargs):
        asyncio.run(original_chat(**kwargs))
    
    def sync_ask(**kwargs):
        asyncio.run(original_ask(**kwargs))
    
    def sync_compact(**kwargs):
        asyncio.run(original_compact(**kwargs))
    
    # Replace command callbacks
    chat.callback = sync_chat
    ask.callback = sync_ask
    compact.callback = sync_compact
    
    cli()


if __name__ == "__main__":
    main()
