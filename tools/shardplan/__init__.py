"""
Smart-sharding allocator for betting arbitrage strategy nodes.

Measures per-sport instrument weights from live node status probes, bin-packs whole
sports into capacity-bounded shard nodes, and emits deployable manifests derived from
the checked-in per-sport shard template.

"""
