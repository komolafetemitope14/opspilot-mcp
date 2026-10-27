"""OpsPilot MCP server."""

import os

from opspilot.audit import AuditedMCPServer, AuditMiddleware, configure_audit_stderr

from opspilot.redaction import RedactionMiddleware, configure_stderr, registry

from opspilot.tools.diagnostics import register_diagnostic_tools
from opspilot.tools.docker import register_docker_tools
from opspilot.tools.kubernetes import register_kubernetes_tools
from opspilot.tools.postgres import register_postgres_tools


registry.register_environment(os.environ)
mcp = AuditedMCPServer("OpsPilot", middleware=[RedactionMiddleware()])
# The public list is outermost-first: include SDK request-state rejections.
mcp.middleware.insert(0, AuditMiddleware())

register_postgres_tools(mcp)
register_docker_tools(mcp)
register_kubernetes_tools(mcp)
register_diagnostic_tools(mcp)


if __name__ == "__main__":
    configure_stderr()
    configure_audit_stderr()
    mcp.run(transport="stdio")
