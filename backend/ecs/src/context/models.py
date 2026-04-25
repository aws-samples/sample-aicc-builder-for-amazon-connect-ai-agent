"""
Session Context Data Models

Defines the data structures for session state management.
"""

from dataclasses import dataclass, field, asdict, fields
from typing import Dict, List, Any, Optional
from datetime import datetime
import json


@dataclass
class SessionContext:
    """
    Unified session context for AICC Builder.

    Tracks all state across the interview and generation phases.
    """
    session_id: str
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Phase tracking
    phase: str = "interview"  # interview | confirmation | generating | review | complete
    turn_count: int = 0
    ready_to_generate: bool = False

    # Collected requirements (from Interviewer)
    company_name: Optional[str] = None
    industry: Optional[str] = None
    agent_name: str = "AI Assistant"
    language: str = ""
    db_type: str = "dynamodb"
    personality: str = "friendly"
    tone: str = "professional"

    # Operations
    operations: List[Dict[str, Any]] = field(default_factory=list)
    escalation_triggers: List[str] = field(default_factory=list)

    # Generation progress
    generated_assets: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Structure: {"lambda": {"status": "completed", "files": [...]}, "openapi": {...}, ...}

    # Current generation step (for resumption)
    current_generation_step: Optional[str] = None  # lambda | openapi | prompt | contact_flow

    # Completeness metrics (0-100)
    completeness: Dict[str, int] = field(default_factory=lambda: {
        "company_info": 0,
        "operations_defined": 0,
        "operations_detailed": 0,
        "overall": 0,
    })

    # Document mode (for questionnaire upload)
    document_mode: bool = False
    uploaded_document: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionContext":
        """Create from dictionary, ignoring unknown fields."""
        valid_fields = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)

    @classmethod
    def from_json(cls, json_str: str) -> "SessionContext":
        """Create from JSON string."""
        return cls.from_dict(json.loads(json_str))

    def get_operation_ids(self) -> List[str]:
        """Get list of operation IDs."""
        return [op.get("operation_id", "") for op in self.operations if op.get("operation_id")]

    def get_operation(self, operation_id: str) -> Optional[Dict[str, Any]]:
        """Get operation by ID."""
        for op in self.operations:
            if op.get("operation_id") == operation_id:
                return op
        return None

    def update_operation(self, operation_id: str, updates: Dict[str, Any]) -> None:
        """Update operation by ID (merge)."""
        for i, op in enumerate(self.operations):
            if op.get("operation_id") == operation_id:
                self.operations[i] = {**op, **updates}
                return
        # If not found, add new operation
        self.operations.append({"operation_id": operation_id, **updates})

    def mark_asset_generated(
        self,
        asset_type: str,
        status: str = "completed",
        files: Optional[List[str]] = None,
        error: Optional[str] = None
    ) -> None:
        """Mark an asset as generated."""
        self.generated_assets[asset_type] = {
            "status": status,
            "files": files or [],
            "error": error,
            "generated_at": datetime.utcnow().isoformat(),
        }

    def is_asset_generated(self, asset_type: str) -> bool:
        """Check if asset has been generated."""
        asset = self.generated_assets.get(asset_type)
        return asset is not None and asset.get("status") == "completed"

    def get_generation_progress(self) -> Dict[str, Any]:
        """Get generation progress summary."""
        steps = ["lambda", "openapi", "prompt", "contact_flow"]
        completed = sum(1 for s in steps if self.is_asset_generated(s))
        return {
            "total_steps": len(steps),
            "completed_steps": completed,
            "current_step": self.current_generation_step,
            "progress_percent": int((completed / len(steps)) * 100),
            "steps": {s: self.generated_assets.get(s, {"status": "pending"}) for s in steps},
        }


@dataclass
class ConversationMessage:
    """Single conversation message."""
    role: str  # "user" | "assistant"
    content: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationMessage":
        valid_fields = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)

    @classmethod
    def from_json(cls, json_str: str) -> "ConversationMessage":
        return cls.from_dict(json.loads(json_str))
