export function serverInstructions(capabilities: string, dashboardUrl?: string): string {
  return (
    "Manages a heterogeneous cluster of Debian/Armbian nodes over SSH.\n" +
    "Call cluster_list_nodes first to learn the available node names, roles, tags and recorded hardware " +
    "(CPU cores/architecture, GPU/accelerators, memory, disks, OS); use cluster_hardware for the full detail or to re-probe. " +
    "Most tools accept a `targets` array of node names, tags, or omit it to hit every node.\n" +
    "These nodes are resource constrained: check the hardware inventory before installing anything, prefer dry runs " +
    "for apt operations, avoid long-running foreground builds, and check free disk space with cluster_status.\n" +
    "Long work should run as a durable background job so it survives SSH drops and the tool-call time limit: " +
    "cluster_packages write actions and module tools whose listed execution is \"optional\" or \"required\" accept " +
    "execution=\"background\" and return a jobId. Poll cluster_get_job (includeResult=true for module output) instead of " +
    "resubmitting. A node rejects new jobs when it is at its concurrent-job limit or short of memory; retry later or " +
    "choose another node rather than forcing the work.\n" +
    "Destructive operations (formatting, reboots, rm -rf, partitioning) require explicit user approval and a confirm flag.\n" +
    "A node with an NVIDIA GPU is enrolled like any other node and then promoted for CUDA separately: use cluster_gpu " +
    "(check, enable, test) rather than installing drivers by hand with cluster_run or cluster_packages.\n" +
    "For approved SMB shares use cluster_list_sources, cluster_check_source, cluster_browse_source, " +
    "cluster_search_source and cluster_stat_source with configured IDs and relative paths. Import selected " +
    "files with cluster_import_source, or use cluster_import_sources with source-relative glob patterns such as " +
    "Screenshot*.png or **/*.jpg for multiple files. Batch results report individual successes and failures; " +
    "pass each successful result.artifact.artifactId to compute modules. To publish a result " +
    "use cluster_export_source with the result artifact ID and a new destination filename. Export is exposed " +
    "only after a configured read-write source passes a write probe; cluster_check_source with writeProbe=true " +
    "refreshes this capability. It never overwrites files. Source transfers are foreground, not durable jobs. " +
    "Never ask for passwords in chat, read credential files, mount SMB on nodes, or bypass source/artifact tools " +
    "with shell commands. On Windows, missing credentials require the user to run scripts/bootstrap.ps1 " +
    "-ConfigureSources under the identity running MCP. On Linux, an administrator must configure an encrypted " +
    "CIFS mount matching sources[].mountPath; verify with npm run sources:check. Never read mount credentials " +
    "or mount shares automatically. Never change firewall rules automatically.\n" +
    (capabilities
      ? "The cluster also runs node modules that do real work for you. Route a request to cluster_call_module_tool " +
        "whenever it matches one of these capabilities, even if the user does not name the module or the node:\n" +
        `${capabilities}\n` +
        "When artifact-storage is installed and available, prefer it for transferring local attachments or files into " +
        "artifact-aware module workflows. For a file exposed through a local path, call cluster_upload_artifact and pass " +
        "its immutable artifact ID to the consuming module. This streams the existing bytes through artifact_upload with " +
        "bounded chunks, integrity checks, quotas, and cleanup. If the MCP client supplies raw base64 but no path, use " +
        "cluster_list_module_tools with toolName=\"artifact_upload\" to get only that schema, then call begin, append, and commit through " +
        "cluster_call_module_tool. For pasted content, first use any client-provided attachment export, download, or " +
        "materialization capability to save the exact bytes to a local temporary file, then call cluster_upload_artifact " +
        "with that path. Do not recreate a file from a rendered preview or vision description. If the client exposes " +
        "neither the original bytes, a local path, nor a way to materialize them, explain that client boundary and ask " +
        "the user to save or attach the content as a file. " +
        "Do not locally compress, convert, summarize, inspect, or otherwise preprocess it merely to reduce transfer size; " +
        "leave requested processing to the artifact-aware module unless the user explicitly asks for local preprocessing. " +
        "Do not use cluster_upload/SFTP, cluster_run, direct node filesystem paths, or the artifact broker socket to " +
        "stage or import artifact content; those paths bypass the artifact API's quotas, integrity checks, and metadata.\n" +
        "These operations are deterministic, bounded and auditable, so prefer them over answering from memory for " +
        "calculation, data analysis, charting, conversion, extraction, redaction, comparison and formatting work. " +
        "Use cluster_list_modules to confirm what is installed. cluster_list_module_tools returns compact operation " +
        "summaries by default; pass toolName to fetch one exact argument schema. Do not request every schema unless needed.\n"
      : "") +
    (dashboardUrl
      ? `Every SSH interaction is logged and shown live on a local dashboard at ${dashboardUrl}. When the user asks ` +
        "to open, view or watch the Vanta monitoring console, call cluster_monitor and then use the MCP client's " +
        "browser-opening capability with the returned URL when one is available. Do not claim the browser was opened " +
        "if the client has no such capability. cluster_list_nodes also reports whether the dashboard is actually running."
      : "")
  );
}