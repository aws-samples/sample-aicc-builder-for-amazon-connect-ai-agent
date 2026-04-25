"""
Contact Flow Generator Tool

Generates Amazon Connect Contact Flow JSON and Mermaid diagram for visualization.
The flow is designed to work with AI agents (Lex Bot with Q in Connect).

Key improvements:
- Uses GetUserInput block with Lex Bot (AMAZON.QinConnectIntent enabled)
- Includes Check Contact Attributes for ESCALATE/COMPLETE tool results
- Properly handles agent transfer with context preservation
"""

import json
import uuid
from typing import Optional
from strands import tool
from .spec_manager import get_all_specs
from .streaming_callback import stream_asset, complete_asset


@tool
def generate_contact_flow(
    flow_name: str,
    company_name: str,
    greeting_message: str = None,
    primary_language: str = "",
    voice_id: str = None,
    include_customer_lookup: bool = True,
    include_customer_phone_lookup: bool = False,
    escalation_queue_name: str = "BasicQueue",
    error_message: str = None,
    ai_bot_type: str = "q_connect",  # "lex" or "q_connect" (default: q_connect for Q in Connect)
    enable_escalation_check: bool = True,
) -> dict:
    """
    Generate an Amazon Connect Contact Flow with Mermaid visualization.

    This tool creates a complete contact flow that:
    1. Sets up language and voice settings
    2. Optionally looks up customer profile
    3. Connects to AI agent via GetUserInput with Lex Bot (AMAZON.QinConnectIntent enabled)
    4. Checks tool results (ESCALATE, COMPLETE) using Check Contact Attributes
    5. Handles agent transfer with conversation context
    6. Handles errors gracefully

    The generated flow includes a Mermaid diagram that can be displayed
    in the chat interface for the user to visualize and request modifications.

    Args:
        flow_name: Name for the contact flow
        company_name: Company name for greeting
        greeting_message: Custom greeting (auto-generated if not provided)
        primary_language: Language code (ko-KR, en-US, ja-JP)
        voice_id: Polly voice ID (auto-selected based on language if not provided)
        include_customer_lookup: Whether to include customer profile lookup
        include_customer_phone_lookup: Whether to include phone-based customer lookup via Lambda + UpdateSessionData for Q in Connect personalization
        escalation_queue_name: Queue name for human escalation
        error_message: Custom error message
        ai_bot_type: Type of AI bot - "lex" or "q_connect" (uses GetUserInput with Lex Bot)
        enable_escalation_check: Enable Check Contact Attributes for ESCALATE tool result

    Returns:
        - contact_flow_json: Complete Contact Flow JSON (importable to Connect)
        - mermaid_diagram: Mermaid flowchart for visualization
        - flow_summary: Human-readable summary of the flow
    """

    # Get operations for context
    specs = get_all_specs()
    operation_names = list(specs.keys()) if specs else []

    # Auto-select voice based on language
    voice_map = {
        "ko-KR": "Seoyeon",
        "en-US": "Matthew",
        "ja-JP": "Takumi",
        "zh-CN": "Zhiyu",
    }
    selected_voice = voice_id or voice_map.get(primary_language, "Matthew")

    # Auto-generate greeting if not provided
    if not greeting_message:
        if primary_language == "ko-KR":
            greeting_message = f"안녕하세요, {company_name}입니다. 무엇을 도와드릴까요?"
        elif primary_language == "ja-JP":
            greeting_message = f"お電話ありがとうございます。{company_name}です。ご用件をお聞かせください。"
        else:
            greeting_message = f"Thank you for calling {company_name}. How can I help you today?"

    # Auto-generate error message if not provided
    if not error_message:
        if primary_language == "ko-KR":
            error_message = "죄송합니다. 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
        elif primary_language == "ja-JP":
            error_message = "申し訳ございません。一時的なエラーが発生しました。しばらくしてからもう一度お試しください。"
        else:
            error_message = "We're sorry, a temporary error occurred. Please try again later."

    # Generate unique IDs for actions
    action_ids = {
        "entry": _generate_id(),
        "set_language": _generate_id(),
        "create_assistant_session": _generate_id(),
        "update_contact_data": _generate_id(),
        "set_voice": _generate_id(),
        "customer_lookup": _generate_id(),
        "phone_lookup": _generate_id(),  # Lambda: customer lookup by phone
        "set_customer_attrs": _generate_id(),  # Store customer info as contact attributes
        "update_q_session": _generate_id(),  # Lambda: UpdateSessionData for Q in Connect
        "get_user_input": _generate_id(),  # GetUserInput with Lex Bot
        "check_tool_result": _generate_id(),  # Check Contact Attributes for tool selection
        "set_escalation_attrs": _generate_id(),  # Set escalation context attributes
        "set_working_queue": _generate_id(),  # Set working queue to BasicQueue
        "escalation": _generate_id(),  # Transfer to agent queue
        "error_handler": _generate_id(),
        "disconnect": _generate_id(),
    }

    # Build actions list
    actions = []

    # 1. Set Language
    actions.append({
        "Parameters": {
            "Language": primary_language
        },
        "Identifier": action_ids["set_language"],
        "Type": "UpdateContactAttributes",
        "Transitions": {
            "NextAction": action_ids["create_assistant_session"],
            "Errors": [{
                "NextAction": action_ids["error_handler"],
                "ErrorType": "NoMatchingError"
            }]
        }
    })

    # 2. CreateWisdomSession
    actions.append({
        "Parameters": {"WisdomAssistantArn": "{{WISDOM_ASSISTANT_ARN}}"},
        "Identifier": action_ids["create_assistant_session"],
        "Type": "CreateWisdomSession",
        "Transitions": {
            "NextAction": action_ids["update_contact_data"],
            "Errors": [{"NextAction": action_ids["set_voice"], "ErrorType": "NoMatchingError"}]
        }
    })

    # 3. UpdateContactData
    actions.append({
        "Parameters": {"WisdomSessionArn": "$.Wisdom.SessionArn"},
        "Identifier": action_ids["update_contact_data"],
        "Type": "UpdateContactData",
        "Transitions": {
            "NextAction": action_ids["set_voice"],
            "Errors": [{"NextAction": action_ids["set_voice"], "ErrorType": "NoMatchingError"}]
        }
    })

    # 4. Set Voice
    # Determine chain: voice → customer_lookup? → phone_lookup chain? → get_user_input
    if include_customer_lookup:
        next_after_voice = action_ids["customer_lookup"]
    elif include_customer_phone_lookup:
        next_after_voice = action_ids["phone_lookup"]
    else:
        next_after_voice = action_ids["get_user_input"]
    actions.append({
        "Parameters": {
            "TextToSpeechVoice": selected_voice,
            "TextToSpeechEngine": "Generative",
            "TextToSpeechStyle": "None"
        },
        "Identifier": action_ids["set_voice"],
        "Type": "UpdateContactTextToSpeechVoice",
        "Transitions": {
            "NextAction": next_after_voice,
            "Errors": [{
                "NextAction": action_ids["error_handler"],
                "ErrorType": "NoMatchingError"
            }]
        }
    })

    # 3. Customer Profile Lookup (optional)
    if include_customer_lookup:
        next_after_customer_lookup = action_ids["phone_lookup"] if include_customer_phone_lookup else action_ids["get_user_input"]
        actions.append({
            "Parameters": {
                "FlowModuleId": "{{CUSTOMER_PROFILE_MODULE_ARN}}"
            },
            "Identifier": action_ids["customer_lookup"],
            "Type": "InvokeFlowModule",
            "Transitions": {
                "NextAction": next_after_customer_lookup,
                "Conditions": [
                    {
                        "NextAction": next_after_customer_lookup,
                        "Condition": {
                            "Operator": "Equals",
                            "Operands": ["Profile found"]
                        }
                    },
                    {
                        "NextAction": next_after_customer_lookup,
                        "Condition": {
                            "Operator": "Equals",
                            "Operands": ["No profile found"]
                        }
                    }
                ],
                "Errors": [
                    {
                        "NextAction": next_after_customer_lookup,
                        "ErrorType": "NoMatchingCondition"
                    },
                    {
                        "NextAction": action_ids["error_handler"],
                        "ErrorType": "NoMatchingError"
                    }
                ]
            }
        })

    # 3b. Phone-based Customer Lookup + Q in Connect Session Update (optional)
    if include_customer_phone_lookup:
        # Lambda: lookup customer by phone number
        actions.append({
            "Parameters": {
                "LambdaFunctionARN": "{{CUSTOMER_LOOKUP_LAMBDA_ARN}}",
                "InvocationTimeLimitSeconds": "8",
                "ResponseValidation": {"ResponseType": "STRING_MAP"}
            },
            "Identifier": action_ids["phone_lookup"],
            "Type": "InvokeLambdaFunction",
            "Transitions": {
                "NextAction": action_ids["set_customer_attrs"],
                "Errors": [{"NextAction": action_ids["get_user_input"], "ErrorType": "NoMatchingError"}]
            }
        })
        # Store customer info as contact attributes
        actions.append({
            "Parameters": {
                "Attributes": {
                    "customerName": "$.External.customerName",
                    "membershipTier": "$.External.membershipTier"
                }
            },
            "Identifier": action_ids["set_customer_attrs"],
            "Type": "UpdateContactAttributes",
            "Transitions": {
                "NextAction": action_ids["update_q_session"],
                "Errors": [{"NextAction": action_ids["get_user_input"], "ErrorType": "NoMatchingError"}]
            }
        })
        # Lambda: UpdateSessionData to inject customer info into Q in Connect session
        actions.append({
            "Parameters": {
                "LambdaFunctionARN": "{{UPDATE_Q_SESSION_LAMBDA_ARN}}",
                "InvocationTimeLimitSeconds": "8",
                "ResponseValidation": {"ResponseType": "STRING_MAP"},
                "LambdaInvocationAttributes": {
                    "customerName": "$.Attributes.customerName",
                    "membershipTier": "$.Attributes.membershipTier"
                }
            },
            "Identifier": action_ids["update_q_session"],
            "Type": "InvokeLambdaFunction",
            "Transitions": {
                "NextAction": action_ids["get_user_input"],
                "Errors": [{"NextAction": action_ids["get_user_input"], "ErrorType": "NoMatchingError"}]
            }
        })

    # 4. GetUserInput with Lex Bot (AMAZON.QinConnectIntent enabled)
    # This is the correct way to integrate Q in Connect - using GetUserInput with a Lex Bot
    # that has the AMAZON.QinConnectIntent enabled for AI self-service
    next_after_input = action_ids["check_tool_result"] if enable_escalation_check else action_ids["disconnect"]

    actions.append({
        "Parameters": {
            "Text": greeting_message,
            "LexV2Bot": {
                "AliasArn": "{{LEX_BOT_ALIAS_ARN}}"  # Lex Bot with AMAZON.QinConnectIntent
            },
            "LexSessionAttributes": {
                "locale": primary_language,
                "contactId": "$.ContactId",
                "customerId": "$.Attributes.customerId"
            }
        },
        "Identifier": action_ids["get_user_input"],
        "Type": "GetUserInput",
        "Transitions": {
            "NextAction": next_after_input,
            "Conditions": [
                {
                    "NextAction": next_after_input,
                    "Condition": {
                        "Operator": "Equals",
                        "Operands": ["AMAZON.QinConnectIntent"]
                    }
                },
                {
                    "NextAction": next_after_input,
                    "Condition": {
                        "Operator": "Equals",
                        "Operands": ["AMAZON.FallbackIntent"]
                    }
                }
            ],
            "Errors": [
                {
                    "NextAction": action_ids["error_handler"],
                    "ErrorType": "NoMatchingError"
                },
                {
                    "NextAction": action_ids["error_handler"],
                    "ErrorType": "NoMatchingCondition"
                },
                {
                    "NextAction": action_ids["error_handler"],
                    "ErrorType": "InputTimeLimitExceeded"
                }
            ]
        }
    })

    # 5. Check Contact Attributes for tool result (ESCALATION, COMPLETE)
    if enable_escalation_check:
        actions.append({
            "Parameters": {
                "Attribute": {
                    "Type": "Lex",
                    "Key": "Tool"
                },
                "Conditions": [
                    {
                        "Operator": "Equals",
                        "Operands": ["ESCALATION"]
                    },
                    {
                        "Operator": "Equals",
                        "Operands": ["COMPLETE"]
                    }
                ]
            },
            "Identifier": action_ids["check_tool_result"],
            "Type": "CheckContactAttributes",
            "Transitions": {
                "NextAction": action_ids["get_user_input"],  # Continue conversation if no tool selected
                "Conditions": [
                    {
                        "NextAction": action_ids["set_escalation_attrs"],
                        "Condition": {
                            "Operator": "Equals",
                            "Operands": ["ESCALATION"]
                        }
                    },
                    {
                        "NextAction": action_ids["disconnect"],
                        "Condition": {
                            "Operator": "Equals",
                            "Operands": ["COMPLETE"]
                        }
                    }
                ],
                "Errors": [
                    {
                        "NextAction": action_ids["get_user_input"],  # Continue if no tool match
                        "ErrorType": "NoMatchingCondition"
                    },
                    {
                        "NextAction": action_ids["error_handler"],
                        "ErrorType": "NoMatchingError"
                    }
                ]
            }
        })

        # 6. Set Escalation Context Attributes (preserve conversation context for agent)
        actions.append({
            "Parameters": {
                "Attributes": {
                    "escalationReason": "$.Lex.escalationReason",
                    "escalationSummary": "$.Lex.escalationSummary",
                    "customerIntent": "$.Lex.customerIntent",
                    "sentiment": "$.Lex.sentiment"
                }
            },
            "Identifier": action_ids["set_escalation_attrs"],
            "Type": "UpdateContactAttributes",
            "Transitions": {
                "NextAction": action_ids["set_working_queue"],
                "Errors": [
                    {
                        "NextAction": action_ids["set_working_queue"],  # Still transfer even if attrs fail
                        "ErrorType": "NoMatchingError"
                    }
                ]
            }
        })

        # 6b. Set Working Queue to BasicQueue
        actions.append({
            "Parameters": {
                "QueueId": "{{BASIC_QUEUE_ARN}}"
            },
            "Identifier": action_ids["set_working_queue"],
            "Type": "UpdateContactTargetQueue",
            "Transitions": {
                "NextAction": action_ids["escalation"],
                "Errors": [
                    {
                        "NextAction": action_ids["escalation"],
                        "ErrorType": "NoMatchingError"
                    }
                ]
            }
        })

    # 7. Escalation to human agent (Transfer to Queue)
    actions.append({
        "Parameters": {
            "QueueId": "{{" + escalation_queue_name.upper().replace(" ", "_") + "_QUEUE_ARN}}"
        },
        "Identifier": action_ids["escalation"],
        "Type": "TransferContactToQueue",
        "Transitions": {
            "NextAction": action_ids["disconnect"],
            "Errors": [{
                "NextAction": action_ids["error_handler"],
                "ErrorType": "NoMatchingError"
            }]
        }
    })

    # 8. Error Handler
    actions.append({
        "Parameters": {
            "Text": error_message
        },
        "Identifier": action_ids["error_handler"],
        "Type": "MessageParticipant",
        "Transitions": {
            "NextAction": action_ids["disconnect"],
            "Errors": [{
                "NextAction": action_ids["disconnect"],
                "ErrorType": "NoMatchingError"
            }]
        }
    })

    # 9. Disconnect
    actions.append({
        "Parameters": {},
        "Identifier": action_ids["disconnect"],
        "Type": "DisconnectParticipant",
        "Transitions": {}
    })

    # Build metadata with positions for visual layout
    metadata = _build_metadata(
        flow_name=flow_name,
        action_ids=action_ids,
        include_customer_lookup=include_customer_lookup,
        include_customer_phone_lookup=include_customer_phone_lookup,
        primary_language=primary_language,
        selected_voice=selected_voice,
        ai_bot_type=ai_bot_type,
        enable_escalation_check=enable_escalation_check
    )

    # Complete Contact Flow JSON
    contact_flow = {
        "Version": "2019-10-30",
        "StartAction": action_ids["set_language"],
        "Metadata": metadata,
        "Actions": actions
    }

    # Generate Mermaid diagram
    mermaid = _generate_mermaid(
        flow_name=flow_name,
        company_name=company_name,
        greeting_message=greeting_message,
        include_customer_lookup=include_customer_lookup,
        include_customer_phone_lookup=include_customer_phone_lookup,
        ai_bot_type=ai_bot_type,
        escalation_queue_name=escalation_queue_name,
        operation_names=operation_names,
        enable_escalation_check=enable_escalation_check
    )

    # Generate summary
    summary = _generate_summary(
        flow_name=flow_name,
        company_name=company_name,
        primary_language=primary_language,
        selected_voice=selected_voice,
        include_customer_lookup=include_customer_lookup,
        ai_bot_type=ai_bot_type,
        operation_names=operation_names,
        enable_escalation_check=enable_escalation_check
    )

    # Stream the generated content (is_complete=True saves to S3 for persistence)
    contact_flow_json_str = json.dumps(contact_flow, indent=2, ensure_ascii=False)
    stream_asset("contact_flow", f"{flow_name}.json", contact_flow_json_str, is_complete=True)
    stream_asset("contact_flow", f"{flow_name}_diagram.md", mermaid, is_complete=True)
    stream_asset("contact_flow", f"{flow_name}_summary.md", summary, is_complete=True)

    return {
        "success": True,
        "flow_name": flow_name,
        "contact_flow_json": contact_flow_json_str,
        "mermaid_diagram": mermaid,
        "flow_summary": summary,
        "placeholders": [
            "{{LEX_BOT_ALIAS_ARN}} - Lex Bot with AMAZON.QinConnectIntent enabled",
            "{{WISDOM_ASSISTANT_ARN}} - Connect Assistant domain ARN",
            "{{CUSTOMER_PROFILE_MODULE_ARN}}" if include_customer_lookup else None,
            "{{BASIC_QUEUE_ARN}} - BasicQueue ARN for escalation",
            "{{CUSTOMER_LOOKUP_LAMBDA_ARN}} - Customer lookup Lambda ARN" if include_customer_phone_lookup else None,
            "{{UPDATE_Q_SESSION_LAMBDA_ARN}} - Update Q Session Lambda ARN" if include_customer_phone_lookup else None,
            "{{" + escalation_queue_name.upper().replace(" ", "_") + "_QUEUE_ARN}}"
        ],
        "configuration": {
            "language": primary_language,
            "voice": selected_voice,
            "ai_bot_type": ai_bot_type,
            "includes_customer_lookup": include_customer_lookup,
            "escalation_check_enabled": enable_escalation_check
        },
        "notes": [
            "This flow uses GetUserInput with a Lex Bot that has AMAZON.QinConnectIntent enabled",
            "The Lex Bot handles Q in Connect AI self-service interactions",
            "Check Contact Attributes block monitors for ESCALATION and COMPLETE tool selections",
            "Escalation context (reason, summary, intent, sentiment) is preserved for agents"
        ]
    }


@tool
def update_contact_flow_greeting(
    current_flow_json: str,
    new_greeting: str
) -> dict:
    """
    Update the greeting message in an existing contact flow.

    Use this when the user wants to modify the greeting without regenerating
    the entire flow.

    Args:
        current_flow_json: The current contact flow JSON string
        new_greeting: The new greeting message

    Returns:
        Updated contact flow JSON and Mermaid diagram
    """
    try:
        flow = json.loads(current_flow_json)

        # Find and update the GetUserInput action (or legacy AI agent action)
        for action in flow.get("Actions", []):
            if action.get("Type") in ["GetUserInput", "ConnectParticipantWithLexBot", "ConnectParticipantWithQConnect"]:
                action["Parameters"]["Text"] = new_greeting
                break

        return {
            "success": True,
            "contact_flow_json": json.dumps(flow, indent=2, ensure_ascii=False),
            "message": "Greeting message updated successfully"
        }
    except json.JSONDecodeError as e:
        return {
            "success": False,
            "error": f"Invalid JSON: {str(e)}"
        }


@tool
def generate_flow_mermaid_only(
    flow_description: str,
    operations: list[str] = None,
    include_error_handling: bool = True,
    include_escalation: bool = True,
    primary_language: str = "ko-KR"
) -> dict:
    """
    Generate only a Mermaid diagram for flow visualization without the full JSON.

    Use this during the conversation to quickly show the user what the flow
    will look like before generating the complete Contact Flow JSON.

    Args:
        flow_description: Brief description of what the flow should do
        operations: List of operations/intents the AI agent can handle
        include_error_handling: Whether to show error handling in diagram
        include_escalation: Whether to show escalation path
        primary_language: Language for labels

    Returns:
        Mermaid diagram string that can be rendered in the frontend
    """
    if not operations:
        specs = get_all_specs()
        operations = list(specs.keys()) if specs else ["handleRequest"]

    # Build Mermaid diagram
    lines = [
        "```mermaid",
        "flowchart TD",
        "    %% Contact Flow Diagram",
        f"    %% {flow_description}",
        ""
    ]

    # Entry point
    if primary_language == "ko-KR":
        lines.append("    A[📞 고객 전화 수신] --> B[🌐 언어 설정]")
        lines.append("    B --> C[🎤 음성 설정]")
        lines.append("    C --> D{👤 고객 조회}")
        lines.append("    D -->|프로필 있음| E[🤖 AI 에이전트]")
        lines.append("    D -->|프로필 없음| E")
    else:
        lines.append("    A[📞 Incoming Call] --> B[🌐 Set Language]")
        lines.append("    B --> C[🎤 Set Voice]")
        lines.append("    C --> D{👤 Customer Lookup}")
        lines.append("    D -->|Profile Found| E[🤖 AI Agent]")
        lines.append("    D -->|No Profile| E")

    # AI Agent handling operations
    lines.append("")
    lines.append("    subgraph AI[AI Agent]")

    for i, op in enumerate(operations[:5]):  # Limit to 5 for readability
        op_label = _format_operation_label(op, primary_language)
        lines.append(f"        E --> F{i}[{op_label}]")

    lines.append("    end")
    lines.append("")

    # Outcomes
    if primary_language == "ko-KR":
        lines.append("    E -->|요청 완료| G[✅ 통화 종료]")
        if include_escalation:
            lines.append("    E -->|상담원 연결| H[👨‍💼 상담원 연결]")
            lines.append("    H --> G")
    else:
        lines.append("    E -->|Request Complete| G[✅ End Call]")
        if include_escalation:
            lines.append("    E -->|Agent Transfer| H[👨‍💼 Transfer to Agent]")
            lines.append("    H --> G")

    # Error handling
    if include_error_handling:
        lines.append("")
        if primary_language == "ko-KR":
            lines.append("    B -.->|오류| ERR[❌ 오류 안내]")
            lines.append("    C -.->|오류| ERR")
            lines.append("    D -.->|오류| ERR")
            lines.append("    ERR --> G")
        else:
            lines.append("    B -.->|Error| ERR[❌ Error Message]")
            lines.append("    C -.->|Error| ERR")
            lines.append("    D -.->|Error| ERR")
            lines.append("    ERR --> G")

    # Styling
    lines.append("")
    lines.append("    style A fill:#e1f5fe")
    lines.append("    style E fill:#fff3e0")
    lines.append("    style G fill:#e8f5e9")
    if include_error_handling:
        lines.append("    style ERR fill:#ffebee")

    lines.append("```")

    return {
        "success": True,
        "mermaid_diagram": "\n".join(lines),
        "operations_shown": operations[:5],
        "message": "Flow diagram generated. You can modify this by asking me to change specific parts."
    }


def _generate_id() -> str:
    """Generate a UUID for action identifier."""
    return str(uuid.uuid4())


def _format_operation_label(operation: str, language: str) -> str:
    """Format operation name for display."""
    # Convert camelCase to readable format
    readable = ""
    for char in operation:
        if char.isupper() and readable:
            readable += " "
        readable += char

    # Add emoji based on operation type
    op_lower = operation.lower()
    if "create" in op_lower or "book" in op_lower:
        return f"➕ {readable}"
    elif "get" in op_lower or "search" in op_lower or "find" in op_lower:
        return f"🔍 {readable}"
    elif "update" in op_lower or "modify" in op_lower:
        return f"✏️ {readable}"
    elif "cancel" in op_lower or "delete" in op_lower:
        return f"❌ {readable}"
    else:
        return f"⚡ {readable}"


def _build_metadata(
    flow_name: str,
    action_ids: dict,
    include_customer_lookup: bool,
    include_customer_phone_lookup: bool,
    primary_language: str,
    selected_voice: str,
    ai_bot_type: str,
    enable_escalation_check: bool = True
) -> dict:
    """Build metadata for contact flow with visual positions."""

    # Calculate positions for visual layout
    x_start = 40
    y_start = 40
    x_spacing = 280
    y_spacing = 120

    action_metadata = {}

    # Calculate x positions based on flow configuration
    x_pos = 0
    positions = [("set_language", x_pos, 0)]
    x_pos += 1
    positions.append(("create_assistant_session", x_pos, 0))
    x_pos += 1
    positions.append(("update_contact_data", x_pos, 0))
    x_pos += 1
    positions.append(("set_voice", x_pos, 0))
    x_pos += 1

    if include_customer_lookup:
        positions.append(("customer_lookup", x_pos, 0))
        x_pos += 1

    if include_customer_phone_lookup:
        positions.append(("phone_lookup", x_pos, 0))
        x_pos += 1
        positions.append(("set_customer_attrs", x_pos, 0))
        x_pos += 1
        positions.append(("update_q_session", x_pos, 0))
        x_pos += 1

    positions.append(("get_user_input", x_pos, 0))
    x_pos += 1

    if enable_escalation_check:
        positions.append(("check_tool_result", x_pos, 0))
        x_pos += 1
        positions.append(("set_escalation_attrs", x_pos, 1))
        positions.append(("set_working_queue", x_pos, 2))

    positions.append(("escalation", x_pos, 3))
    positions.append(("error_handler", 2, 3))
    positions.append(("disconnect", x_pos + 1, 0))

    for action_name, x_idx, y_idx in positions:
        if action_name in action_ids:
            action_metadata[action_ids[action_name]] = {
                "position": {
                    "x": x_start + (x_idx * x_spacing),
                    "y": y_start + (y_idx * y_spacing)
                }
            }

    # Add friendly names
    if "set_language" in action_ids:
        action_metadata[action_ids["set_language"]]["isFriendlyName"] = True
    if "get_user_input" in action_ids:
        action_metadata[action_ids["get_user_input"]]["isFriendlyName"] = True
    # Add set_voice language attribute metadata
    if "set_voice" in action_ids and action_ids["set_voice"] in action_metadata:
        action_metadata[action_ids["set_voice"]]["parameters"] = {
            "TextToSpeechVoice": {"languageCode": primary_language or "en-US"}
        }
        action_metadata[action_ids["set_voice"]]["overrideConsoleVoice"] = False
    # Add create_assistant_session children/fragments metadata
    if "create_assistant_session" in action_ids and action_ids["create_assistant_session"] in action_metadata:
        action_metadata[action_ids["create_assistant_session"]]["isFriendlyName"] = True
        action_metadata[action_ids["create_assistant_session"]]["children"] = [action_ids["update_contact_data"]]
        action_metadata[action_ids["create_assistant_session"]]["parameters"] = {"WisdomAssistantArn": {"displayName": ""}}
        action_metadata[action_ids["create_assistant_session"]]["fragments"] = {"SetContactData": action_ids["update_contact_data"]}
    if "update_contact_data" in action_ids and action_ids["update_contact_data"] in action_metadata:
        action_metadata[action_ids["update_contact_data"]]["dynamicParams"] = []

    return {
        "entryPointPosition": {"x": x_start - 200, "y": y_start},
        "ActionMetadata": action_metadata,
        "name": flow_name,
        "description": f"AI Contact Flow - Q in Connect via Lex Bot - {primary_language}",
        "type": "contactFlow",
        "status": "DRAFT",
        "hash": {}
    }


def _generate_mermaid(
    flow_name: str,
    company_name: str,
    greeting_message: str,
    include_customer_lookup: bool,
    include_customer_phone_lookup: bool,
    ai_bot_type: str,
    escalation_queue_name: str,
    operation_names: list[str],
    enable_escalation_check: bool = True
) -> str:
    """Generate Mermaid flowchart diagram."""

    # Escape special characters in greeting
    safe_greeting = greeting_message[:30] + "..." if len(greeting_message) > 30 else greeting_message
    safe_greeting = safe_greeting.replace('"', "'")

    lines = [
        "```mermaid",
        "flowchart TD",
        f"    %% {flow_name} - {company_name}",
        f"    %% Q in Connect AI Self-Service Flow",
        "",
        "    START((Start)) --> LANG[Set Language]",
        "    LANG --> VOICE[Set Voice]",
    ]

    if include_customer_lookup and include_customer_phone_lookup:
        lines.extend([
            "    VOICE --> LOOKUP{Customer Profile<br/>Lookup}",
            "    LOOKUP -->|Found| PHONELOOKUP",
            "    LOOKUP -->|Not Found| PHONELOOKUP",
            "    LOOKUP -.->|Error| ERROR",
            "    PHONELOOKUP[📞 Customer Lookup<br/>by Phone] --> SETCUST[Set Customer<br/>Attributes]",
            "    SETCUST --> UPDATESESS[Update Q Session] --> INPUT",
        ])
    elif include_customer_lookup:
        lines.extend([
            "    VOICE --> LOOKUP{Customer Profile<br/>Lookup}",
            "    LOOKUP -->|Found| INPUT",
            "    LOOKUP -->|Not Found| INPUT",
            "    LOOKUP -.->|Error| ERROR",
        ])
    elif include_customer_phone_lookup:
        lines.extend([
            "    VOICE --> PHONELOOKUP[📞 Customer Lookup<br/>by Phone]",
            "    PHONELOOKUP --> SETCUST[Set Customer<br/>Attributes]",
            "    SETCUST --> UPDATESESS[Update Q Session] --> INPUT",
        ])
    else:
        lines.append("    VOICE --> INPUT")

    # GetUserInput with Lex Bot section
    lines.extend([
        "",
        f"    INPUT[🎤 GetUserInput<br/>Lex Bot + QinConnect<br/>\"{safe_greeting}\"]",
        "",
    ])

    # Operations subgraph
    if operation_names:
        lines.append("    subgraph OPS[MCP Tools / Operations]")
        for op in operation_names[:6]:
            label = _format_operation_label(op, "en-US")
            lines.append(f"        {op}[{label}]")
        lines.append("    end")
        lines.append("    INPUT --> OPS")
        lines.append("")

    # Check Tool Result (if enabled)
    if enable_escalation_check:
        lines.extend([
            "    INPUT --> CHECK{Check Tool<br/>Result}",
            "    CHECK -->|COMPLETE| END[✅ End Call]",
            "    CHECK -->|ESCALATION| ATTRS[Set Escalation<br/>Attributes]",
            "    CHECK -->|Continue| INPUT",
            "    ATTRS --> SETQ[Set Working Queue<br/>BasicQueue]",
            "    SETQ --> QUEUE",
            "",
        ])
    else:
        lines.extend([
            "    INPUT -->|Complete| END[✅ End Call]",
            "    INPUT -->|Transfer| QUEUE",
            "",
        ])

    # Escalation Queue
    lines.extend([
        f"    QUEUE[👨‍💼 Transfer to<br/>{escalation_queue_name}]",
        "    QUEUE --> END",
        "",
        "    %% Error Handling",
        "    LANG -.->|Error| ERROR[❌ Error Message]",
        "    VOICE -.->|Error| ERROR",
        "    INPUT -.->|Error| ERROR",
        "    ERROR --> END",
        "",
        "    %% Styling",
        "    style START fill:#4CAF50,color:#fff",
        "    style INPUT fill:#2196F3,color:#fff",
        "    style END fill:#9E9E9E,color:#fff",
        "    style ERROR fill:#f44336,color:#fff",
        "    style QUEUE fill:#FF9800,color:#fff",
    ])

    if enable_escalation_check:
        lines.append("    style CHECK fill:#9C27B0,color:#fff")
        lines.append("    style ATTRS fill:#607D8B,color:#fff")
        lines.append("    style SETQ fill:#607D8B,color:#fff")

    lines.append("```")

    return "\n".join(lines)


def _generate_summary(
    flow_name: str,
    company_name: str,
    primary_language: str,
    selected_voice: str,
    include_customer_lookup: bool,
    ai_bot_type: str,
    operation_names: list[str],
    enable_escalation_check: bool = True
) -> str:
    """Generate human-readable flow summary."""

    lang_names = {
        "ko-KR": "한국어",
        "en-US": "English",
        "ja-JP": "日本語",
        "zh-CN": "中文"
    }

    summary_lines = [
        f"## Contact Flow: {flow_name}",
        "",
        f"**Company:** {company_name}",
        f"**Language:** {lang_names.get(primary_language, primary_language)}",
        f"**Voice:** {selected_voice} (Generative)",
        f"**AI Integration:** Lex Bot with AMAZON.QinConnectIntent (Q in Connect)",
        "",
        "### Flow Architecture:",
        "This flow uses the recommended pattern for Q in Connect integration:",
        "- **GetUserInput** block with a Lex Bot that has AMAZON.QinConnectIntent enabled",
        "- **Check Contact Attributes** to monitor tool selections (ESCALATION, COMPLETE)",
        "- **Escalation context preservation** for seamless agent handoff",
        "",
        "### Flow Steps:",
        "1. Set language and voice settings",
    ]

    step = 2
    if include_customer_lookup:
        summary_lines.append(f"{step}. Look up customer profile from Customer Profiles")
        step += 1

    summary_lines.append(f"{step}. GetUserInput with Lex Bot (Q in Connect AI handles conversation)")
    step += 1

    if operation_names:
        summary_lines.append(f"{step}. AI can use these MCP tools/operations:")
        for op in operation_names:
            summary_lines.append(f"   - {op}")
        step += 1

    if enable_escalation_check:
        summary_lines.append(f"{step}. Check tool result (ESCALATION triggers agent transfer, COMPLETE ends call)")
        step += 1
        summary_lines.append(f"{step}. On ESCALATION: Set context attributes and transfer to queue")
        step += 1

    summary_lines.extend([
        f"{step}. End call",
        "",
        "### Placeholders to Replace:",
        "- `{{LEX_BOT_ALIAS_ARN}}` - Lex Bot ARN with AMAZON.QinConnectIntent enabled",
    ])

    if include_customer_lookup:
        summary_lines.append("- `{{CUSTOMER_PROFILE_MODULE_ARN}}` - Customer profile flow module ARN")

    summary_lines.extend([
        "- `{{QUEUE_ARN}}` - Escalation queue ARN",
        "",
        "### Escalation Context:",
        "When AI triggers ESCALATION, these attributes are passed to the agent:",
        "- `escalationReason`: Why the escalation occurred",
        "- `escalationSummary`: Summary of the conversation",
        "- `customerIntent`: What the customer wants",
        "- `sentiment`: Customer's emotional state",
    ])

    return "\n".join(summary_lines)
