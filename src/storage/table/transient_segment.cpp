#include "duckdb/storage/table/transient_segment.hpp"
#include "duckdb/common/types/null_value.hpp"
#include "duckdb/common/types/vector.hpp"
#include "duckdb/common/vector_operations/vector_operations.hpp"
#include "duckdb/storage/numeric_segment.hpp"
#include "duckdb/storage/string_segment.hpp"
#include "duckdb/storage/table/append_state.hpp"
#include "duckdb/storage/table/persistent_segment.hpp"
#include "duckdb/storage/storage_manager.hpp"

namespace duckdb {

TransientSegment::TransientSegment(DatabaseInstance &db, LogicalType type, idx_t start)
    : ColumnSegment(type, ColumnSegmentType::TRANSIENT, start), db(db) {
	if (type.InternalType() == PhysicalType::VARCHAR) {
		data = make_unique<StringSegment>(db, start);
	} else {
		data = make_unique<NumericSegment>(db, type.InternalType(), start);
	}
}

TransientSegment::TransientSegment(PersistentSegment &segment)
    : ColumnSegment(segment.type, ColumnSegmentType::TRANSIENT, segment.start), db(segment.db) {
	if (segment.block_id == segment.data->block->BlockId()) {
		segment.data->ToTemporary();
	}
	data = move(segment.data);
	stats = move(segment.stats);
	count = segment.count;
	D_ASSERT(!segment.next);
}

void TransientSegment::InitializeScan(ColumnScanState &state) {
	data->InitializeScan(state);
}

void TransientSegment::Scan(Transaction &transaction, ColumnScanState &state, idx_t vector_index, Vector &result) {
	data->Scan(transaction, state, vector_index, result);
}

void TransientSegment::ScanCommitted(ColumnScanState &state, idx_t vector_index, Vector &result) {
	data->ScanCommitted(state, vector_index, result);
}

void TransientSegment::FilterScan(Transaction &transaction, ColumnScanState &state, Vector &result,
                                  SelectionVector &sel, idx_t &approved_tuple_count) {
	data->FilterScan(transaction, state, result, sel, approved_tuple_count);
}

void TransientSegment::IndexScan(ColumnScanState &state, Vector &result) {
	data->IndexScan(state, state.vector_index, result);
}

void TransientSegment::Select(Transaction &transaction, ColumnScanState &state, Vector &result, SelectionVector &sel,
                              idx_t &approved_tuple_count, vector<TableFilter> &table_filter) {
	return data->Select(transaction, result, table_filter, sel, approved_tuple_count, state);
}

void TransientSegment::Fetch(ColumnScanState &state, idx_t vector_index, Vector &result) {
	data->Fetch(state, vector_index, result);
}

void TransientSegment::FetchRow(ColumnFetchState &state, Transaction &transaction, row_t row_id, Vector &result,
                                idx_t result_idx) {
	data->FetchRow(state, transaction, row_id - this->start, result, result_idx);
}

void TransientSegment::Update(ColumnData &column_data, Transaction &transaction, Vector &updates, row_t *ids,
                              idx_t count) {
	data->Update(column_data, stats, transaction, updates, ids, count, this->start);
}

void TransientSegment::InitializeAppend(ColumnAppendState &state) {
	state.lock = data->lock.GetExclusiveLock();
}

idx_t TransientSegment::Append(ColumnAppendState &state, Vector &append_data, idx_t offset, idx_t count) {
	idx_t appended = data->Append(stats, append_data, offset, count);
	this->count += appended;
	return appended;
}

void TransientSegment::RevertAppend(idx_t start_row) {
	data->tuple_count = start_row - this->start;
	this->count = start_row - this->start;
}

} // namespace duckdb
