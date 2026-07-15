"""Architecture spec input schema, mirroring Input_Specification's sections 1-6.

Used as the typed boundary for spec_versions rows. Scribe diffs two instances
of this model via model_dump() rather than raw dicts, so diff paths line up
with actual spec fields instead of whatever shape a hand-built dict happens
to have.
"""
from pydantic import BaseModel, Field


class ProjectOverview(BaseModel):
    purpose: str
    target_users: str
    deployment_environment: str


class FunctionalRequirements(BaseModel):
    core_features: list[str] = Field(default_factory=list)
    key_user_flows: list[str] = Field(default_factory=list)
    integration_points: list[str] = Field(default_factory=list)


class NonFunctionalRequirements(BaseModel):
    performance: str | None = None
    scalability: str | None = None
    availability: str | None = None
    security: str | None = None
    observability: str | None = None


class TechnicalConstraints(BaseModel):
    language_framework: list[str] = Field(default_factory=list)
    existing_systems: list[str] = Field(default_factory=list)
    budget_infra_limits: str | None = None
    team_skillset: str | None = None


class DataArchitecture(BaseModel):
    data_sources: list[str] = Field(default_factory=list)
    storage_requirements: str | None = None
    data_flow: str | None = None
    retention_compliance: str | None = None


class ArchitectureSpec(BaseModel):
    """Top-level spec. spec_version is set by the caller (pipeline_runs /
    spec_versions table), not part of the user-submitted content itself."""
    project_overview: ProjectOverview
    functional_requirements: FunctionalRequirements
    non_functional_requirements: NonFunctionalRequirements
    technical_constraints: TechnicalConstraints
    data_architecture: DataArchitecture
    open_questions: list[str] = Field(default_factory=list)
