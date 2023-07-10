//===----------------------------------------------------------------------===//
//                         DuckDB
//
// duckdb/execution/radix_partitioned_hashtable.hpp
//
//
//===----------------------------------------------------------------------===//

#pragma once

#include "duckdb/execution/operator/aggregate/grouped_aggregate_data.hpp"
#include "duckdb/parser/group_by_node.hpp"

namespace duckdb {

class GroupedAggregateHashTable;
struct AggregatePartition;
struct MaterializedAggregateData;
enum class RadixHTAbandonType : uint8_t;

class RadixPartitionedHashTable {
public:
	RadixPartitionedHashTable(GroupingSet &grouping_set, const GroupedAggregateData &op);

	GroupingSet &grouping_set;
	//! The indices specified in the groups_count that do not appear in the grouping_set
	unsafe_vector<idx_t> null_groups;
	const GroupedAggregateData &op;

	vector<LogicalType> group_types;

	//! The GROUPING values that belong to this hash table
	vector<Value> grouping_values;

public:
	//! Sink Interface
	unique_ptr<GlobalSinkState> GetGlobalSinkState(ClientContext &context) const;
	unique_ptr<LocalSinkState> GetLocalSinkState(ExecutionContext &context) const;

	void Sink(ExecutionContext &context, DataChunk &chunk, OperatorSinkInput &input, DataChunk &aggregate_input_chunk,
	          const unsafe_vector<idx_t> &filter) const;
	void Combine(ExecutionContext &context, GlobalSinkState &gstate, LocalSinkState &lstate) const;
	void Finalize(ClientContext &context, GlobalSinkState &gstate) const;

	//! Source interface
	idx_t Count(GlobalSinkState &sink) const;
	unique_ptr<GlobalSourceState> GetGlobalSourceState(ClientContext &context) const;
	unique_ptr<LocalSourceState> GetLocalSourceState(ExecutionContext &context) const;
	SourceResultType GetData(ExecutionContext &context, DataChunk &chunk, GlobalSinkState &sink,
	                         OperatorSourceInput &input) const;

	static void SetMultiScan(GlobalSinkState &sink);
	// TODO: capacity
	unique_ptr<GroupedAggregateHashTable> CreateHT(ClientContext &context, const idx_t radix_bits) const;

private:
	idx_t CountInternal(GlobalSinkState &sink) const;
	void SetGroupingValues();
	void PopulateGroupChunk(DataChunk &group_chunk, DataChunk &input_chunk) const;

	bool CombineInternal(ClientContext &context, GlobalSinkState &gstate, LocalSinkState &lstate,
	                     const RadixHTAbandonType local_abandon_type) const;
	bool ShouldCombine(ClientContext &context, GlobalSinkState &gstate_p, const idx_t sink_partition_idx,
	                   const RadixHTAbandonType local_abandon_type) const;
	void CombinePartition(AggregatePartition &partition, vector<MaterializedAggregateData> &uncombined_data) const;

	static bool RequiresRepartitioning(ClientContext &context, GlobalSinkState &gstate);
};

} // namespace duckdb
