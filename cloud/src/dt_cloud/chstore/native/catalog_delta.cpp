// C++17 signed first-hit bucket totals for many literals at once (`mega_catalog`).
//
//   catalog-delta TERMS < rows > totals
//
// TERMS: the literals, each as a RowBinary String (varuint length, bytes),
// numbered from 0 in file order: lowercase, nonempty, slash- and NUL-free.
// stdin: RowBinary rows `(lpath String, bucket String, sign Int8, size Int64,
// n_files Int64)` — a version's lowercase path (`lowerUTF8(path)`), its first
// path segment as stored, and the version's weights, opened (+1) or closed (−1).
// stdout: a JSON header listing the buckets seen (in first-seen order), one
// `term<TAB>bucket<TAB>Σ sign·size<TAB>Σ sign·n_files` line per nonzero
// (term, bucket) total, and a completion footer with the row count.
//
// A row is a first hit for a literal when its name (the last segment of
// lpath) contains it and its parent path (lpath before the last '/') does not:
// a slash-free literal matches within one segment, so "the parent contains
// it" is exactly "an ancestor's name matches", and that ancestor's rollup
// already covers the row (`mega_names` / `hot_l1.oracle`'s rule). Matching is
// bytewise; UTF-8 is self-synchronizing, so a valid literal matches only at
// code-point boundaries, as ClickHouse's `position` and `LIKE` do.
//
// One Aho–Corasick automaton holds every literal. A parent's matches are
// marked once per distinct parent (rows arrive in `(depth, path)` order, so
// siblings are adjacent), a name's matches once per row.
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

[[noreturn]] static void fail(const std::string& message) { throw std::runtime_error(message); }

class Input {
    std::FILE* file;
    std::vector<unsigned char> buffer = std::vector<unsigned char>(16 << 20);
    size_t pos = 0, end = 0;
    bool fill() {
        end = std::fread(buffer.data(), 1, buffer.size(), file);
        pos = 0;
        if (!end && std::ferror(file)) fail("input read failed");
        return end != 0;
    }
public:
    explicit Input(std::FILE* f) : file(f) {}
    bool eof() { return pos == end && !fill(); }
    uint8_t byte() {
        if (pos == end && !fill()) fail("truncated input");
        return buffer[pos++];
    }
    uint64_t varuint() {
        uint64_t value = 0;
        for (unsigned i = 0; i < 10; ++i) {
            const uint8_t part = byte();
            value |= uint64_t(part & 127) << (i * 7);
            if (!(part & 128)) return value;
        }
        fail("invalid RowBinary string length");
    }
    void string(std::string& out) {
        const uint64_t n = varuint();
        if (n > (64u << 20)) fail("string longer than 64 MiB");
        out.resize(n);
        size_t at = 0;
        while (at < n) {
            if (pos == end && !fill()) fail("truncated input");
            const size_t take = std::min<size_t>(n - at, end - pos);
            std::memcpy(out.data() + at, buffer.data() + pos, take);
            pos += take, at += take;
        }
    }
    uint64_t u64() {
        uint64_t value = 0;
        for (unsigned i = 0; i < 8; ++i) value |= uint64_t(byte()) << (i * 8);
        return value;
    }
};

// Aho–Corasick over bytes: goto edges in one open-addressing table keyed by
// (state, byte), the root's 256 edges dense, failure and dictionary-suffix links.
class Automaton {
    static constexpr uint64_t EMPTY = ~uint64_t(0);
    std::vector<uint64_t> keys;
    std::vector<int32_t> values;
    std::vector<std::vector<std::pair<uint8_t, int32_t>>> children{{}};
    int32_t root[256];
    static uint64_t mix(uint64_t x) {
        x ^= x >> 33;
        x *= 0xff51afd7ed558ccdull;
        x ^= x >> 33;
        return x;
    }
    int32_t edge(int32_t s, uint8_t c) const {
        const uint64_t key = uint64_t(s) << 8 | c;
        const size_t mask = keys.size() - 1;
        for (size_t i = mix(key) & mask;; i = (i + 1) & mask) {
            if (keys[i] == key) return values[i];
            if (keys[i] == EMPTY) return -1;
        }
    }
public:
    std::vector<int32_t> out{-1}, link{0}, dict{-1};
    size_t terms = 0;

    void add(std::string_view term, int32_t id) {
        int32_t s = 0;
        for (const unsigned char c : term) {
            int32_t next = -1;
            for (const auto& [b, child] : children[s]) if (b == c) { next = child; break; }
            if (next < 0) {
                next = static_cast<int32_t>(out.size());
                children[s].emplace_back(c, next);
                children.emplace_back();
                out.push_back(-1), link.push_back(0), dict.push_back(-1);
            }
            s = next;
        }
        if (out[s] >= 0) fail("duplicate term");
        out[s] = id;
        ++terms;
    }

    void build() {
        size_t edges = 0;
        for (const auto& c : children) edges += c.size();
        size_t cap = 16;
        while (cap < edges * 2 + 2) cap *= 2;
        keys.assign(cap, EMPTY), values.assign(cap, -1);
        const size_t mask = cap - 1;
        for (size_t s = 0; s < children.size(); ++s)
            for (const auto& [c, child] : children[s]) {
                const uint64_t key = uint64_t(s) << 8 | c;
                size_t i = mix(key) & mask;
                while (keys[i] != EMPTY) i = (i + 1) & mask;
                keys[i] = key, values[i] = child;
            }
        for (int c = 0; c < 256; ++c) root[c] = 0;
        std::vector<int32_t> queue;
        for (const auto& [c, child] : children[0]) root[c] = child, queue.push_back(child);
        for (size_t q = 0; q < queue.size(); ++q) {
            const int32_t u = queue[q];
            for (const auto& [c, v] : children[u]) {
                int32_t f = link[u];
                while (f && edge(f, c) < 0) f = link[f];
                const int32_t g = f ? edge(f, c) : root[c];
                link[v] = g >= 0 && g != v ? g : 0;
                dict[v] = out[link[v]] >= 0 ? link[v] : dict[link[v]];
                queue.push_back(v);
            }
        }
        children.clear();
        children.shrink_to_fit();
    }

    int32_t step(int32_t s, uint8_t c) const {
        while (s) {
            const int32_t t = edge(s, c);
            if (t >= 0) return t;
            s = link[s];
        }
        return root[c];
    }

    // Every term occurring in `text` (once per occurrence; callers dedupe).
    template <class F> void scan(std::string_view text, F emit) const {
        int32_t s = 0;
        for (const unsigned char c : text) {
            s = step(s, c);
            for (int32_t x = out[s] >= 0 ? s : dict[s]; x >= 0; x = dict[x]) emit(out[x]);
        }
    }
};

int main(int argc, char** argv) try {
    if (argc != 2) fail("usage: catalog-delta TERMS < rows");
    std::FILE* terms_file = std::fopen(argv[1], "rb");
    if (!terms_file) fail("cannot open terms file");
    Automaton ac;
    {
        Input in(terms_file);
        std::string term;
        int32_t id = 0;
        while (!in.eof()) {
            in.string(term);
            if (term.empty() || term.find('/') != std::string::npos || term.find('\0') != std::string::npos) fail("invalid term");
            ac.add(term, id++);
        }
    }
    std::fclose(terms_file);
    ac.build();
    const size_t n = ac.terms;

    std::vector<std::string> buckets;
    std::vector<std::vector<int64_t>> sums;  // per bucket: 2 per term (bytes, objects)
    std::vector<uint64_t> name_mark(n, 0), parent_mark(n, 0);
    uint64_t row = 0, parent_epoch = 0;
    std::string lpath, bucket, parent;
    bool have_parent = false;
    Input in(stdin);
    while (!in.eof()) {
        in.string(lpath);
        in.string(bucket);
        const int8_t sign = static_cast<int8_t>(in.byte());
        const int64_t size = static_cast<int64_t>(in.u64());
        const int64_t files = static_cast<int64_t>(in.u64());
        if (sign != 1 && sign != -1) fail("sign must be ±1");
        ++row;
        size_t b = 0;
        while (b < buckets.size() && buckets[b] != bucket) ++b;
        if (b == buckets.size()) {
            if (buckets.size() >= 4096) fail("more than 4096 buckets");
            buckets.push_back(bucket);
            sums.emplace_back(2 * n, 0);
        }
        const size_t cut = lpath.rfind('/');
        const std::string_view parent_view = cut == std::string::npos ? std::string_view() : std::string_view(lpath).substr(0, cut);
        const std::string_view name = cut == std::string::npos ? std::string_view(lpath) : std::string_view(lpath).substr(cut + 1);
        if (!have_parent || parent_view != parent) {
            parent.assign(parent_view);
            have_parent = true;
            ++parent_epoch;
            ac.scan(parent, [&](int32_t t) { parent_mark[t] = parent_epoch; });
        }
        std::vector<int64_t>& acc = sums[b];
        ac.scan(name, [&](int32_t t) {
            if (name_mark[t] == row) return;
            name_mark[t] = row;
            if (parent_mark[t] == parent_epoch) return;
            acc[2 * t] += sign * size;
            acc[2 * t + 1] += sign * files;
        });
    }

    std::string out = "{\"schema\": \"catalog-delta-v1\", \"buckets\": [";
    for (size_t b = 0; b < buckets.size(); ++b) {
        out += b ? ", \"" : "\"";
        for (const unsigned char c : buckets[b]) {
            if (c == '"' || c == '\\') out.push_back('\\'), out.push_back(static_cast<char>(c));
            else if (c < 0x20) {
                char buf[8];
                std::snprintf(buf, sizeof buf, "\\u%04x", c);
                out += buf;
            } else out.push_back(static_cast<char>(c));
        }
        out += "\"";
    }
    out += "]}\n";
    std::fwrite(out.data(), 1, out.size(), stdout);
    char line[96];
    for (size_t b = 0; b < buckets.size(); ++b)
        for (size_t t = 0; t < n; ++t) {
            const int64_t bytes = sums[b][2 * t], objects = sums[b][2 * t + 1];
            if (!bytes && !objects) continue;
            const int len = std::snprintf(line, sizeof line, "%zu\t%zu\t%lld\t%lld\n", t, b, (long long)bytes, (long long)objects);
            std::fwrite(line, 1, len, stdout);
        }
    std::printf("{\"complete\": true, \"rows\": %llu}\n", (unsigned long long)row);
    return 0;
} catch (const std::exception& e) {
    std::fprintf(stderr, "catalog-delta: %s\n", e.what());
    return 1;
}
