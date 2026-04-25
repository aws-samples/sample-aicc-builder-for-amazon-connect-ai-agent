"""
Infrastructure Generator Sub-Agent

Generates AWS CloudFormation YAML templates for infrastructure components:
- DynamoDB tables with GSIs
- Sample data seeding with Custom Resources
- IAM roles and policies

Simpler than CDK - just deploy with: aws cloudformation deploy
"""

from .agent import infrastructure_generator_agent, set_callback_handler

__all__ = ["infrastructure_generator_agent", "set_callback_handler"]
