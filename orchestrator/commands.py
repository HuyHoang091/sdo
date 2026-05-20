"""
Orchestration Commands - Data models for Core Agent to Service Agent communication.

This module defines the command types and data structures used in the hierarchical
multi-agent RL architecture for service orchestration.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class CommandType(Enum):
    """
    Types of orchestration commands from Core Agent to Service Agents.
    
    Each command type represents a different orchestration context that
    influences how Service Agents should process and execute actions.
    
    Attributes:
        SCALE_UP_RECOMMENDED: Indicates the service should consider scaling up.
            Used when the Core Agent detects increased load or capacity needs.
        SCALE_DOWN_ALLOWED: Indicates the service is allowed to scale down.
            Used when the Core Agent confirms no upstream bottlenecks exist.
        BOTTLENECK_DETECTED: Indicates a bottleneck service has been identified.
            Service Agents should adjust behavior based on their relation to the bottleneck.
        UPSTREAM_RECOVERING: Indicates upstream services are recovering from stress.
            Service Agents should avoid scale_down actions during this period.
        NORMAL_OPERATION: Standard operation with no special constraints.
            Service Agents can proceed with normal RL-based decision making.
        EMERGENCY_PRIORITY: Critical priority requiring immediate action.
            Service Agents should bypass normal cooldowns and execute recommended actions.
    """
    SCALE_UP_RECOMMENDED = "scale_up_recommended"
    SCALE_DOWN_ALLOWED = "scale_down_allowed"
    BOTTLENECK_DETECTED = "bottleneck_detected"
    UPSTREAM_RECOVERING = "upstream_recovering"
    NORMAL_OPERATION = "normal_operation"
    EMERGENCY_PRIORITY = "emergency_priority"


@dataclass
class OrchestrationCommand:
    """
    Command from Core Agent to Service Agent specifying orchestration context.
    
    This dataclass represents a directive from the centralized Core Agent to a
    Service Agent, providing context about the current fleet state, bottleneck
    status, and recommended actions.
    
    Attributes:
        target_service: The service this command is addressed to.
        command_type: Type of orchestration directive (e.g., SCALE_UP_RECOMMENDED).
        bottleneck_service: Current bottleneck service name, if any.
        cascade_impact: List of services affected by cascade from bottleneck.
        recommended_action: Suggested action type (0-6), if any.
            0: no_op, 1: scale_up, 2: scale_down, 3: restart_unhealthy,
            4: increase_cpu_limit, 5: increase_mem_limit, 6: kill_and_replace
        context: Additional context (fleet state, thresholds, etc.).
        timestamp: When the command was generated.
    
    Example:
        >>> cmd = OrchestrationCommand(
        ...     target_service="api",
        ...     command_type=CommandType.SCALE_UP_RECOMMENDED,
        ...     bottleneck_service="database",
        ...     cascade_impact=["api", "web-app"],
        ...     recommended_action=1
        ... )
        >>> cmd.validate()
        True
        >>> cmd.to_dict()["target_service"]
        'api'
    """
    target_service: str
    command_type: CommandType
    bottleneck_service: Optional[str] = None
    cascade_impact: List[str] = field(default_factory=list)
    recommended_action: Optional[int] = None
    context: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize command to dictionary for logging/transmission.
        
        Converts the OrchestrationCommand to a JSON-serializable dictionary
        format suitable for logging, transmission over network, or storage.
        
        Returns:
            Dictionary containing all command fields with:
            - command_type as its string value
            - timestamp as ISO format string
            
        Example:
            >>> cmd = OrchestrationCommand(
            ...     target_service="api",
            ...     command_type=CommandType.BOTTLENECK_DETECTED
            ... )
            >>> d = cmd.to_dict()
            >>> d["command_type"]
            'bottleneck_detected'
            >>> "timestamp" in d
            True
        """
        return {
            "target_service": self.target_service,
            "command_type": self.command_type.value,
            "bottleneck_service": self.bottleneck_service,
            "cascade_impact": self.cascade_impact,
            "recommended_action": self.recommended_action,
            "context": self.context,
            "timestamp": self.timestamp.isoformat(),
        }
    
    def validate(self) -> bool:
        """
        Validate command structure and parameters.
        
        Checks that the command has all required fields with valid values:
        - target_service must be non-empty
        - recommended_action must be in range 0-6 if specified
        
        Returns:
            True if command is valid, False otherwise.
            
        Example:
            >>> cmd = OrchestrationCommand(
            ...     target_service="api",
            ...     command_type=CommandType.NORMAL_OPERATION,
            ...     recommended_action=1
            ... )
            >>> cmd.validate()
            True
            >>> invalid_cmd = OrchestrationCommand(
            ...     target_service="",
            ...     command_type=CommandType.NORMAL_OPERATION
            ... )
            >>> invalid_cmd.validate()
            False
        """
        if not self.target_service:
            return False
        if self.recommended_action is not None:
            if not 0 <= self.recommended_action <= 6:
                return False
        return True


@dataclass
class CommandResult:
    """
    Result of command execution from Service Agent back to Core Agent.
    
    This dataclass represents the outcome of processing an OrchestrationCommand
    by a Service Agent, including whether the command was successful, what action
    was executed (if any), and the reason if the action was blocked.
    
    Attributes:
        service: The service that processed the command.
        success: Whether the command was processed successfully.
        action_executed: Name of the action that was executed, if any.
            One of: no_op, scale_up, scale_down, restart_unhealthy,
            increase_cpu_limit, increase_mem_limit, kill_and_replace.
        blocked_reason: Reason the action was blocked, if applicable.
            Common reasons include: cooldown_active, limits_exceeded,
            guardrail_violation, upstream_bottleneck.
        timestamp: When the result was generated.
    
    Example:
        >>> result = CommandResult(
        ...     service="api",
        ...     success=True,
        ...     action_executed="scale_up"
        ... )
        >>> result.success
        True
        >>> result.service
        'api'
        
        >>> blocked = CommandResult(
        ...     service="web-app",
        ...     success=False,
        ...     blocked_reason="cooldown_active"
        ... )
        >>> blocked.success
        False
        >>> blocked.blocked_reason
        'cooldown_active'
    """
    service: str
    success: bool
    action_executed: Optional[str] = None
    blocked_reason: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
