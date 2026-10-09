#!/bin/bash
# Per term: matching names, distinct postings rows (versions), suffix-range rows (versions x occurrences),
# trigram list sizes (names containing each trigram), and granules of m_nodes today's layout touches.
set -eu
while read -r q; do
  e=$(printf '%s' "$q" | sed "s/'/\\\\'/g")
  r=$(clickhouse-client --max_threads 32 -q "
    WITH '$e' AS q
    SELECT count(), sum(v), sum(v * countSubstrings(l, q)), sum(c)
    FROM (SELECT l, sum(versions) v, sum(closed) c FROM default.name_spans WHERE position(l, '$e') > 0 GROUP BY l) FORMAT TSV")
  g=$(clickhouse-client --max_threads 32 -q "
    SELECT uniqExact(intDiv(_part_offset, 256)) FROM default.m_nodes
    WHERE name IN (SELECT DISTINCT l FROM default.name_spans WHERE position(l, '$e') > 0) FORMAT TSV")
  t=$(clickhouse-client --max_threads 32 -q "
    WITH arrayDistinct(arrayMap(i -> substringUTF8('$e', i, 3), range(1, toUInt64(greatest(lengthUTF8('$e') - 1, 2))))) AS gs
    SELECT concat(toString(min(n)), ',', toString(max(n)), ',', toString(count())) FROM (
      SELECT g, count() n FROM (SELECT DISTINCT l FROM default.name_spans) ARRAY JOIN gs AS g WHERE position(l, g) > 0 GROUP BY g
    ) FORMAT TSV")
  printf '%s\t%s\t%s\t%s\n' "$q" "$r" "$g" "$t"
done
