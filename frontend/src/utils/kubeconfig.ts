/** What the add-cluster form can tell from a pasted kubeconfig before it is
 *  sent: a name to suggest and the namespace its context names. */
import { load } from 'js-yaml'

export interface KubeconfigFacts {
  /** A machine-name-safe suggestion; '' when nothing useful is in it. */
  name: string
  namespace: string
  server: string
}

/** Cluster names kubeadm and installers use for every cluster: no help in
 *  telling two apart. */
const GENERIC = new Set(['kubernetes', 'default', 'cluster', 'local', 'kind'])

function safe(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64)
}

export function readKubeconfig(text: string): KubeconfigFacts {
  let doc: any
  try {
    doc = load(text)
  } catch {
    return { name: '', namespace: '', server: '' }
  }
  if (!doc || typeof doc !== 'object') return { name: '', namespace: '', server: '' }
  const contexts: any[] = Array.isArray(doc.contexts) ? doc.contexts : []
  const context = contexts.find((c) => c?.name === doc['current-context']) ?? contexts[0]
  const clusterName = String(context?.context?.cluster ?? '')
  const clusters: any[] = Array.isArray(doc.clusters) ? doc.clusters : []
  const cluster = clusters.find((c) => c?.name === clusterName) ?? clusters[0]
  const server = String(cluster?.cluster?.server ?? '')
  const host = /^https?:\/\/([^:/\s]+)/.exec(server)?.[1] ?? ''
  // An address names a cluster better than a generic cluster name; a DNS name
  // by its first label (gpu-a.example.com → gpu-a), an IP whole.
  const fromHost = /^\d+(\.\d+){3}$/.test(host) ? host : host.split('.')[0]
  const name = clusterName && !GENERIC.has(clusterName.toLowerCase())
    ? safe(clusterName) : safe(fromHost)
  return {
    name: name === 'kubernetes' ? '' : name,
    namespace: String(context?.context?.namespace ?? '').trim(),
    server,
  }
}
