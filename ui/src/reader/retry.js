/* retry — what to send again after a page failed. A request that already names its node
 * (Deeper, Simpler, Regenerate, back/forward) is resent as it was, so a retry does the same
 * job. A fresh open resends {node_id} once the node event gave us the node, else its body. */
export function retryBody(request, nodeId) {
  if (request && request.node_id) return request
  return nodeId ? { node_id: nodeId } : request
}
