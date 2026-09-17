export function serverInstructions(capabilities: string, dashboardUrl?: string): string {
  return (
    "Manages a heterogeneous cluster of Debian/Armbian nodes over SSH.\n" +
    "Call cluster_list_nodes first to learn the available node names, roles, tags and recorded hardware " +
    "(CPU cores/architecture, GPU/accelerators, memory, disks, OS); use cluster_hardware for the full detail or to re-probe. " +
    "Most tools accept a `targets` array of node names, tags, or omit it to hit every node.\n" +
    "These nodes are resource constrained: check the hardware inventory before installing anything, prefer dry runs " +
    "for apt operations, avoid long-running foreground builds, and check free disk space with cluster_status.\n" +
    "Destructive operations (formatting, reboots, rm -rf, partitioning) require explicit user approval and a confirm flag.\n" +
    (capabilities
      ? "The cluster also runs node modules that do real work for you. Route a request to cluster_call_module_tool " +
        "whenever it matches one of these capabilities, even if the user does not name the module or the node:\n" +
        `${capabilities}\n` +
        "When artifact-storage is installed and available, prefer it for transferring local attachments or files into " +
        "artifact-aware module workflows. For a file exposed through a local path, call cluster_upload_artifact and pass " +
        "its immutable artifact ID to the consuming module. This streams the existing bytes through artifact_upload with " +
        "bounded chunks, integrity checks, quotas, and cleanup. If the MCP client supplies raw base64 but no path, use " +
        "cluster_list_module_tools to get the artifact_upload schema, then call begin, append, and commit through " +
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
        "Use cluster_list_modules to confirm what is installed, and cluster_list_module_tools for exact operation names " +
        "and argument schemas.\n"
      : "") +
    (dashboardUrl
      ? `Every SSH interaction is logged and shown live on a local dashboard at ${dashboardUrl} - mention it when the ` +
        "user asks what you did, wants to watch progress, or is debugging. cluster_list_nodes reports whether it is " +
        "actually running."
      : "")
  );
}