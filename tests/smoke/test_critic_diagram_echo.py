# tests/smoke/test_critic_diagram_echo.py
from agents.critic import (
    _flag_duplicate_list_items,
    _flag_cross_field_duplication,
    _flag_diagram_relationship_echo,
)
from schemas.architect import Component

def test_spofs_flagged_as_diagram_echo_on_873af0ae_data():
    diagram = '''
    Rel(customer, rag_system, "Sends support queries via Support Chat Widget")
    Rel(rag_system, confluence, "Retrieves source documentation")
    '''
    components = [
        Component(id="customer", name="External Customer", type="person",
                   redundant=False, description="", technology=None),
        Component(id="rag_system", name="RAG System", type="internal_system",
                   redundant=False, description="", technology=None),
        Component(id="confluence", name="Confluence API", type="external_system",
                   redundant=False, description="", technology=None),
    ]
    parsed = {
        "gaps": [],
        "spofs": [
            "RAG System sends support queries via Support Chat Widget to external systems",
            "Support Chat Widget receives support queries from RAG System",
        ],
        "missing_integrations": [],
    }
    echo_fields = _flag_diagram_relationship_echo(parsed, diagram, components)
    assert echo_fields == ["spofs"]
    assert all(s.startswith("POSSIBLE DIAGRAM RELATIONSHIP ECHO") for s in parsed["spofs"])