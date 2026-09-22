# Giving the platform access to a GPU cluster it does not run in

When the platform runs in one cluster and the GPUs are in another, it reaches the
GPU cluster with a kubeconfig you paste into the Resources page. Mint a scoped
one — never hand over an admin credential.

```bash
# on the GPU cluster, once, with an admin kubeconfig
kubectl apply -f backend-rbac.yaml

# mint the scoped kubeconfig the platform will use
./make-scoped-kubeconfig.sh
```

`backend-rbac.yaml` grants one namespace's worth of access plus read-only nodes:
create and read the objects a run is made of, read pods, their logs and their
warning events, and run policy Jobs. Nodes are read to fill in a machine's card
count and type and to resolve a NodePort endpoint; they are never written.

That is deliberately small. A leaked token from it cannot touch production.

The platform encrypts the kubeconfig at rest with its `jwtSecret`, so rotating
that secret makes stored kubeconfigs unreadable and they must be pasted again.

If the platform runs *inside* the GPU cluster, none of this applies: set
`gpuCluster.inCluster=true` and the chart grants its own ServiceAccount the same
access directly.
