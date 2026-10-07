// C++17 frozen paired L2 first-hit aggregation. Each dated source is sorted by pre.
// Accepted per-date prefix proofs are required externally: this protocol validates
// declared bucket/frame parents, not an undeclared deeper parent's presence.
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <iostream>
#include <limits>
#include <queue>
#include <stdexcept>
#include <string>
#include <sys/resource.h>
#include <unordered_set>
#include <utility>
#include <vector>

using U128 = unsigned __int128;
constexpr uint32_t NONE = std::numeric_limits<uint32_t>::max();
constexpr uint32_t MAX_QUERIES = 500000, MAX_FRAMES = 500000;
constexpr uint64_t MAX_CELLS = 10000000, MAX_PATTERN_BYTES = 64U << 20;

[[noreturn]] static void fail(const char* message) { throw std::runtime_error(message); }

class Input {
    FILE* file;
    std::vector<unsigned char> buffer = std::vector<unsigned char>(8 << 20);
    size_t pos = 0, end = 0;
public:
    explicit Input(FILE* source) : file(source) {}
    int byte() {
        if (pos == end) {
            end = std::fread(buffer.data(), 1, buffer.size(), file);
            pos = 0;
            if (!end) {
                if (std::ferror(file)) fail("input read failed");
                return -1;
            }
        }
        return buffer[pos++];
    }
    uint8_t required() {
        const int value = byte();
        if (value < 0) fail("truncated input");
        return static_cast<uint8_t>(value);
    }
    uint64_t fixed(unsigned bytes) {
        uint64_t value = 0;
        for (unsigned i = 0; i < bytes; ++i) value |= uint64_t(required()) << (i * 8);
        return value;
    }
    uint64_t varuint() {
        uint64_t value = 0;
        for (unsigned i = 0; i < 10; ++i) {
            const uint8_t part = required();
            if (i == 9 && part > 1) fail("invalid RowBinary string length");
            value |= uint64_t(part & 127) << (i * 7);
            if (!(part & 128)) return value;
        }
        fail("invalid RowBinary string length");
    }
    std::string string() {
        const uint64_t size = varuint();
        if (size > (64U << 20)) fail("RowBinary string exceeds 64 MiB protocol limit");
        std::string value(static_cast<size_t>(size), '\0');
        size_t copied = 0;
        while (copied < size) {
            if (pos == end) {
                value[copied++] = static_cast<char>(required());
                continue;
            }
            const size_t count = std::min(static_cast<size_t>(size) - copied, end - pos);
            std::copy_n(buffer.data() + pos, count, value.begin() + copied);
            pos += count;
            copied += count;
        }
        return value;
    }
};

static bool valid_utf8(const std::string& text) {
    for (size_t i = 0; i < text.size();) {
        const uint8_t first = static_cast<uint8_t>(text[i++]);
        if (first < 128) continue;
        unsigned extra;
        uint32_t code, minimum;
        if (first >= 0xC2 && first <= 0xDF) { extra = 1; code = first & 31; minimum = 0x80; }
        else if (first >= 0xE0 && first <= 0xEF) { extra = 2; code = first & 15; minimum = 0x800; }
        else if (first >= 0xF0 && first <= 0xF4) { extra = 3; code = first & 7; minimum = 0x10000; }
        else return false;
        if (extra > text.size() - i) return false;
        for (unsigned j = 0; j < extra; ++j) {
            const uint8_t next = static_cast<uint8_t>(text[i++]);
            if ((next & 0xC0) != 0x80) return false;
            code = (code << 6) | (next & 63);
        }
        if (code < minimum || code > 0x10FFFF || (code >= 0xD800 && code <= 0xDFFF)) return false;
    }
    return true;
}

struct State {
    std::vector<std::pair<uint8_t, uint32_t>> edges;
    uint32_t fail = 0, output = NONE, terminal = NONE;
};

class Automaton {
    std::vector<State> states = std::vector<State>(1);
    std::array<uint32_t, 256> root{};
    uint32_t edge(uint32_t state, uint8_t character) const {
        if (!state) return root[character];
        const auto& edges = states[state].edges;
        const auto it = std::lower_bound(edges.begin(), edges.end(), character,
            [](const auto& item, uint8_t value) { return item.first < value; });
        return it != edges.end() && it->first == character ? it->second : NONE;
    }
public:
    void add(const std::string& text, uint32_t query) {
        uint32_t state = 0;
        for (uint8_t character : text) {
            uint32_t child = NONE;
            for (const auto& item : states[state].edges)
                if (item.first == character) { child = item.second; break; }
            if (child == NONE) {
                if (states.size() >= NONE) fail("automaton state count exceeds UInt32");
                child = static_cast<uint32_t>(states.size());
                states.emplace_back();
                states[state].edges.emplace_back(character, child);
            }
            state = child;
        }
        states[state].terminal = query;
    }
    void finish() {
        root.fill(NONE);
        for (auto& state : states) std::sort(state.edges.begin(), state.edges.end());
        std::queue<uint32_t> queue;
        for (const auto& item : states[0].edges) { root[item.first] = item.second; queue.push(item.second); }
        while (!queue.empty()) {
            const uint32_t state = queue.front();
            queue.pop();
            for (const auto& item : states[state].edges) {
                uint32_t fallback = states[state].fail;
                uint32_t child = edge(fallback, item.first);
                while (fallback && child == NONE) { fallback = states[fallback].fail; child = edge(fallback, item.first); }
                const uint32_t failed = child == NONE ? 0 : child;
                states[item.second].fail = failed;
                states[item.second].output = states[failed].terminal == NONE ? states[failed].output : failed;
                queue.push(item.second);
            }
        }
    }
    template<typename Hit> void scan(const std::string& text, Hit hit) const {
        uint32_t state = 0;
        for (uint8_t character : text) {
            uint32_t child = edge(state, character);
            while (state && child == NONE) { state = states[state].fail; child = edge(state, character); }
            state = child == NONE ? 0 : child;
            for (uint32_t output = state; output != NONE; output = states[output].output)
                if (states[output].terminal != NONE) hit(states[output].terminal);
        }
    }
};

struct Bounds { uint64_t pre, post; };
struct DeclaredFrame { uint64_t pre, post; uint32_t bucket; };
struct Weights { U128 b = 0, o = 0; };
using Pair = std::array<Weights, 2>;
struct Ancestor {
    uint64_t pre, post, b, o;
    Weights children;
    std::vector<uint32_t> activated;
};
struct Node { uint64_t pre, post, b, o; std::string name; };
struct Cell { uint32_t query, frame; std::array<uint64_t, 4> weights; };

static void add(Weights& total, uint64_t b, uint64_t o) {
    const U128 maximum = ~U128(0);
    if (total.b > maximum - b || total.o > maximum - o) fail("aggregate overflow");
    total.b += b;
    total.o += o;
}

class DateStream {
    Input input;
    uint64_t expected;
public:
    Node node{};
    bool available = false;
    uint64_t rows = 0, processed = 0, previous = 0, peak_stack = 0, peak_active = 0;
    std::vector<Ancestor> stack;
    std::vector<uint32_t> active, active_pos;
    std::vector<Weights> ordinary;
    std::vector<uint8_t> present;
    Weights root;
    unsigned bucket = 0;
    DateStream(FILE* source, uint64_t count, uint32_t queries, unsigned buckets) :
        input(source), expected(count), active_pos(queries, NONE), ordinary(buckets), present(buckets, 0) {}
    void next() {
        if (rows == expected) {
            if (input.byte() != -1) fail("trailing dated input after declared node count");
            available = false;
            return;
        }
        node = {input.fixed(8), input.fixed(8), input.fixed(8), input.fixed(8), input.string()};
        ++rows;
        available = true;
    }
    void pop() {
        const Ancestor& ancestor = stack.back();
        if (ancestor.children.b > ancestor.b || ancestor.children.o > ancestor.o) fail("child rollups exceed their parent");
        for (uint32_t q : ancestor.activated) {
            const uint32_t position = active_pos[q], last = active.back();
            if (position == NONE || position >= active.size()) fail("active predicate stack is inconsistent");
            active[position] = last;
            active_pos[last] = position;
            active.pop_back();
            active_pos[q] = NONE;
        }
        stack.pop_back();
    }
    void finish() {
        while (!stack.empty()) pop();
        Weights sum;
        for (const Weights& bucket : ordinary) {
            if (bucket.b > UINT64_MAX || bucket.o > UINT64_MAX) fail("bucket rollup exceeds UInt64");
            add(sum, static_cast<uint64_t>(bucket.b), static_cast<uint64_t>(bucket.o));
        }
        if (sum.b != root.b || sum.o != root.o) fail("bucket rollups disagree with the global root");
    }
};

static std::string decimal(U128 value) {
    if (!value) return "0";
    std::string result;
    while (value) { result.push_back('0' + value % 10); value /= 10; }
    std::reverse(result.begin(), result.end());
    return result;
}

static uint64_t argument(const char* text) {
    uint64_t value = 0;
    if (!*text) fail("native argument must be an unsigned integer");
    for (; *text; ++text) {
        if (*text < '0' || *text > '9' || value > (UINT64_MAX - (*text - '0')) / 10)
            fail("native argument must be an unsigned integer");
        value = value * 10 + (*text - '0');
    }
    return value;
}

static void run(int left_fd, int right_fd, uint64_t max_cells) {
    Input control(stdin);
    for (char expected : std::string("HL2PAIR1"))
        if (control.required() != expected) fail("invalid paired protocol magic");
    const std::array<uint64_t, 2> nodes{control.fixed(8), control.fixed(8)};
    const uint32_t queries = static_cast<uint32_t>(control.fixed(4));
    const uint32_t frame_count = static_cast<uint32_t>(control.fixed(4));
    const unsigned bucket_count = control.required();
    if (!nodes[0] || !nodes[1] || !queries || queries > MAX_QUERIES || frame_count > MAX_FRAMES)
        fail("paired node/query/frame counts exceed protocol bounds");
    if (bucket_count < 1 || bucket_count > 6) fail("paired protocol requires one to six buckets");
    std::vector<Bounds> bounds;
    for (unsigned j = 0; j < bucket_count; ++j) {
        Bounds bucket{control.fixed(8), control.fixed(8)};
        if (bucket.pre > bucket.post || (!j && bucket.pre != 1) ||
                (j && (bounds.back().post == UINT64_MAX || bucket.pre != bounds.back().post + 1)))
            fail("bucket intervals must partition the ordered global domain");
        bounds.push_back(bucket);
    }
    std::vector<DeclaredFrame> frames;
    std::vector<uint64_t> last(bucket_count);
    for (unsigned j = 0; j < bucket_count; ++j) last[j] = bounds[j].pre;
    for (uint32_t f = 0; f < frame_count; ++f) {
        DeclaredFrame frame{control.fixed(8), control.fixed(8), static_cast<uint32_t>(control.fixed(4))};
        if (frame.bucket >= bucket_count || frame.pre > frame.post || frame.pre <= bounds[frame.bucket].pre ||
                frame.post > bounds[frame.bucket].post || (f && frame.pre <= frames.back().post) ||
                last[frame.bucket] == UINT64_MAX || frame.pre != last[frame.bucket] + 1)
            fail("frames must partition complete ordered bucket descendants");
        last[frame.bucket] = frame.post;
        frames.push_back(frame);
    }
    for (unsigned j = 0; j < bucket_count; ++j)
        if (last[j] != bounds[j].post) fail("frames must partition complete ordered bucket descendants");
    Automaton automaton;
    std::vector<uint64_t> tau;
    uint64_t pattern_bytes = 0;
    {
        std::unordered_set<std::string> registered;
        for (uint32_t q = 0; q < queries; ++q) {
            const std::string pattern = control.string();
            if (pattern.empty() || pattern.find('/') != std::string::npos || pattern.find('\0') != std::string::npos ||
                    !valid_utf8(pattern) || pattern.size() > 2048 ||
                    std::count_if(pattern.begin(), pattern.end(), [](uint8_t c) { return (c & 0xC0) != 0x80; }) > 512)
                fail("queries must be bounded nonempty valid UTF-8 NUL/slash-free literals");
            if (!registered.insert(pattern).second) fail("query literals must be unique");
            for (unsigned bucket = 0; bucket < bucket_count; ++bucket) {
                const uint64_t threshold = control.fixed(8);
                if (!threshold) fail("query threshold must be positive");
                tau.push_back(threshold);
            }
            pattern_bytes += pattern.size();
            if (pattern_bytes > MAX_PATTERN_BYTES) fail("query automaton exceeds its pattern-byte budget");
            automaton.add(pattern, q);
        }
    }
    if (control.byte() != -1) fail("trailing control input");
    automaton.finish();
    FILE* left_file = fdopen(left_fd, "rb");
    FILE* right_file = fdopen(right_fd, "rb");
    if (!left_file || !right_file) fail("dated source descriptor cannot be opened");
    std::array<DateStream, 2> streams{DateStream(left_file, nodes[0], queries, bucket_count),
                                   DateStream(right_file, nodes[1], queries, bucket_count)};
    std::vector<Pair> roots(static_cast<size_t>(queries) * bucket_count), kept(static_cast<size_t>(queries) * bucket_count);
    std::vector<Pair> totals(queries);
    std::vector<uint32_t> epoch(queries, 0), touched;
    std::vector<uint64_t> match_seen(queries, 0);
    std::vector<uint32_t> cached_matches, heavy;
    std::string cached_name;
    bool cache_valid = false;
    uint64_t matcher_scans = 0, cache_hits = 0;
    std::array<Weights, 2> frame_ordinary{};
    std::array<bool, 2> frame_present{};
    std::vector<Cell> cells;
    uint32_t frame_index = 0;
    auto matches = [&](const std::string& name) -> const std::vector<uint32_t>& {
        // Cache only raw distinct name matches. Date-specific ancestor flags and
        // scalar contributions are always applied again by process().
        if (cache_valid && cached_name == name) {
            if (cache_hits == UINT64_MAX) fail("matcher cache counter overflows UInt64");
            ++cache_hits;
            return cached_matches;
        }
        if (matcher_scans == UINT64_MAX) fail("matcher scan counter overflows UInt64");
        ++matcher_scans;
        cached_name = name;
        cache_valid = true;
        cached_matches.clear();
        automaton.scan(name, [&](uint32_t q) {
            if (match_seen[q] == matcher_scans) return;
            match_seen[q] = matcher_scans;
            cached_matches.push_back(q);
        });
        return cached_matches;
    };
    auto frame_add = [&](uint32_t q, unsigned side, uint64_t b, uint64_t o) {
        if (frame_index >= frame_count) fail("contribution lacks a declared frame");
        if (epoch[q] != frame_index + 1) {
            epoch[q] = frame_index + 1;
            totals[q] = {};
            touched.push_back(q);
        }
        add(totals[q][side], b, o);
    };
    auto flush = [&]() {
        if (frame_index >= frame_count) return;
        heavy.clear();
        for (uint32_t q : touched) {
            const Pair& pair = totals[q];
            for (unsigned side = 0; side < 2; ++side)
                if (pair[side].b > frame_ordinary[side].b || pair[side].o > frame_ordinary[side].o)
                    fail("predicate frame totals exceed ordinary rollups");
            if (std::max(pair[0].b, pair[1].b) < tau[static_cast<size_t>(q) * bucket_count + frames[frame_index].bucket]) continue;
            heavy.push_back(q);
        }
        std::sort(heavy.begin(), heavy.end());
        for (uint32_t q : heavy) {
            const Pair& pair = totals[q];
            if (cells.size() >= max_cells) fail("paired heavy-cell output exceeds its cap");
            Cell cell{q, frame_index, {static_cast<uint64_t>(pair[0].b), static_cast<uint64_t>(pair[0].o),
                                     static_cast<uint64_t>(pair[1].b), static_cast<uint64_t>(pair[1].o)}};
            cells.push_back(cell);
            for (unsigned side = 0; side < 2; ++side)
                add(kept[static_cast<size_t>(q) * bucket_count + frames[frame_index].bucket][side], cell.weights[side * 2], cell.weights[side * 2 + 1]);
        }
        touched.clear();
        frame_ordinary = {};
        frame_present = {};
        ++frame_index;
    };
    auto process = [&](unsigned side) {
        DateStream& stream = streams[side];
        const Node& node = stream.node;
        ++stream.processed;
        if (!valid_utf8(node.name) || node.name.find('/') != std::string::npos)
            fail("node names must be valid UTF-8 slash-free strings");
        if (node.pre > node.post || (stream.processed > 1 && node.pre <= stream.previous))
            fail("dated node order or interval bounds are invalid");
        if (node.post > bounds.back().post) fail("dated node lies outside the global domain");
        stream.previous = node.pre;
        if (stream.processed == 1) {
            if (node.pre != 0 || node.post != bounds.back().post || !node.name.empty())
                fail("dated stream must start with the empty-name complete global root");
            stream.root = {node.b, node.o};
            stream.stack.push_back({node.pre, node.post, node.b, node.o, {}, {}});
            stream.peak_stack = 1;
            return;
        }
        while (!stream.stack.empty() && stream.stack.back().post < node.pre) stream.pop();
        if (stream.stack.empty() || node.post > stream.stack.back().post) fail("dated node intervals cross rather than nest");
        Ancestor& parent = stream.stack.back();
        add(parent.children, node.b, node.o);
        if (parent.children.b > parent.b || parent.children.o > parent.o) fail("child rollups exceed their parent");
        while (stream.bucket < bucket_count && node.pre > bounds[stream.bucket].post) ++stream.bucket;
        if (stream.bucket == bucket_count || node.post > bounds[stream.bucket].post) fail("dated node crosses bucket boundaries");
        const bool bucket_root = node.pre == bounds[stream.bucket].pre;
        if (bucket_root) {
            if (node.post != bounds[stream.bucket].post || stream.stack.size() != 1)
                fail("dated bucket root differs from its frozen bounds/parent");
            stream.ordinary[stream.bucket] = {node.b, node.o};
            stream.present[stream.bucket] = 1;
        } else {
            if (!stream.present[stream.bucket]) fail("descendant has no present declared bucket root");
            if (frame_index >= frame_count || node.pre < frames[frame_index].pre || node.post > frames[frame_index].post ||
                    frames[frame_index].bucket != stream.bucket)
                fail("dated node lacks its declared frame interval");
            const DeclaredFrame& frame = frames[frame_index];
            if (node.pre == frame.pre) {
                if (node.post != frame.post || stream.stack.size() != 2 || parent.pre != bounds[stream.bucket].pre)
                    fail("dated frame differs from its frozen bounds/parent");
                frame_present[side] = true;
                frame_ordinary[side] = {node.b, node.o};
                // A matching ancestor contributes the complete dated frame once,
                // even when the frame name itself does not match that predicate.
                for (uint32_t q : stream.active) frame_add(q, side, node.b, node.o);
            } else if (!frame_present[side]) fail("descendant has no present declared frame root");
        }
        const bool leaf = node.pre == node.post;
        std::vector<uint32_t> activated;
        uint64_t new_hits = 0;
        for (uint32_t q : matches(node.name)) {
            if (stream.active_pos[q] != NONE) continue;
            add(roots[static_cast<size_t>(q) * bucket_count + stream.bucket][side], node.b, node.o);
            if (!bucket_root) frame_add(q, side, node.b, node.o);
            ++new_hits;
            if (!leaf) {
                stream.active_pos[q] = static_cast<uint32_t>(stream.active.size());
                stream.active.push_back(q);
                activated.push_back(q);
            }
        }
        stream.peak_active = std::max(stream.peak_active, static_cast<uint64_t>(stream.active.size()) + (leaf ? new_hits : 0));
        if (!leaf) {
            stream.stack.push_back({node.pre, node.post, node.b, node.o, {}, std::move(activated)});
            stream.peak_stack = std::max(stream.peak_stack, static_cast<uint64_t>(stream.stack.size()));
        }
    };
    streams[0].next();
    streams[1].next();
    while (streams[0].available || streams[1].available) {
        if (streams[0].available && streams[1].available && streams[0].node.pre == streams[1].node.pre) {
            if (streams[0].node.post != streams[1].node.post || streams[0].node.name != streams[1].node.name)
                fail("paired node declarations disagree for the same frozen position");
        }
        const unsigned side = !streams[0].available ? 1 : !streams[1].available ? 0 : streams[0].node.pre <= streams[1].node.pre ? 0 : 1;
        const uint64_t merged_pre = streams[side].node.pre;
        for (DateStream& stream : streams)
            while (!stream.stack.empty() && stream.stack.back().post < merged_pre) stream.pop();
        while (frame_index < frame_count && streams[side].node.pre > frames[frame_index].post) flush();
        process(side);
        streams[side].next();
    }
    while (frame_index < frame_count) flush();
    streams[0].finish();
    streams[1].finish();
    if (U128(matcher_scans) + cache_hits != U128(nodes[0]) + nodes[1] - 2)
        fail("matcher counters disagree with complete dated nonroot rows");
    for (uint32_t q = 0; q < queries; ++q)
        for (unsigned bucket = 0; bucket < bucket_count; ++bucket)
            for (unsigned side = 0; side < 2; ++side) {
                const size_t offset = static_cast<size_t>(q) * bucket_count + bucket;
                if (roots[offset][side].b > streams[side].ordinary[bucket].b || roots[offset][side].o > streams[side].ordinary[bucket].o)
                    fail("predicate root totals exceed ordinary bucket rollups");
                if (kept[offset][side].b > roots[offset][side].b || kept[offset][side].o > roots[offset][side].o)
                    fail("heavy frames exceed their exact predicate bucket totals");
            }
    struct rusage usage{};
    if (getrusage(RUSAGE_SELF, &usage) != 0) fail("getrusage failed");
#ifdef __APPLE__
    const uint64_t rss = usage.ru_maxrss;
#else
    const uint64_t rss = static_cast<uint64_t>(usage.ru_maxrss) * 1024;
#endif
    std::cout << "{\"schema\":\"hot-l2-native-pair-v2\",\"exact\":true,\"incremental\":false,\"levels\":2,\"rows_read\":["
              << streams[0].rows << ',' << streams[1].rows << "],\"registered_predicates\":" << queries << ",\"registered_frames\":" << frame_count
              << ",\"peak_stack\":[" << streams[0].peak_stack << ',' << streams[1].peak_stack << "],\"peak_active\":["
              << streams[0].peak_active << ',' << streams[1].peak_active << "],\"max_cells\":" << max_cells << ",\"emitted_cells\":" << cells.size()
              << ",\"native_peak_rss_bytes\":" << rss << ",\"matcher_scans\":" << matcher_scans << ",\"cache_hits\":" << cache_hits << ",\"roots\":[";
    for (uint32_t q = 0; q < queries; ++q) {
        if (q) std::cout << ',';
        std::cout << "{\"predicate_id\":" << uint64_t(q) + 1 << ",\"buckets\":[";
        for (unsigned bucket = 0; bucket < bucket_count; ++bucket) {
            if (bucket) std::cout << ',';
            const Pair& pair = roots[static_cast<size_t>(q) * bucket_count + bucket];
            std::cout << "[\"" << decimal(pair[0].b) << "\",\"" << decimal(pair[0].o) << "\",\""
                      << decimal(pair[1].b) << "\",\"" << decimal(pair[1].o) << "\"]";
        }
        std::cout << "]}";
    }
    std::cout << "],\"cells\":[";
    for (size_t i = 0; i < cells.size(); ++i) {
        if (i) std::cout << ',';
        const Cell& cell = cells[i];
        std::cout << "{\"predicate_id\":" << uint64_t(cell.query) + 1 << ",\"frame_id\":" << uint64_t(cell.frame) + 1
                  << ",\"b\":[\"" << cell.weights[0] << "\",\"" << cell.weights[2] << "\"],\"o\":[\""
                  << cell.weights[1] << "\",\"" << cell.weights[3] << "\"]}";
    }
    std::cout << "]}\n";
    if (!std::cout) fail("output write failed");
}

int main(int argc, char** argv) {
    try {
        uint64_t left = 0, right = 0, max_cells = MAX_CELLS;
        bool supplied_left = false, supplied_right = false, supplied_cap = false;
        for (int i = 1; i < argc; i += 2) {
            if (i + 1 == argc) fail("native arguments require a value");
            const std::string option = argv[i];
            if (option == "--left-fd" && !supplied_left) { left = argument(argv[i + 1]); supplied_left = true; }
            else if (option == "--right-fd" && !supplied_right) { right = argument(argv[i + 1]); supplied_right = true; }
            else if (option == "--max-cells" && !supplied_cap) { max_cells = argument(argv[i + 1]); supplied_cap = true; }
            else fail("unsupported or duplicate native arguments");
        }
        if (!supplied_left || !supplied_right || left < 3 || right < 3 || left == right || left > INT32_MAX || right > INT32_MAX)
            fail("paired sources require two distinct inherited descriptors above stderr");
        if (!max_cells || max_cells > MAX_CELLS) fail("heavy-cell cap must be from 1 to 10000000");
        run(static_cast<int>(left), static_cast<int>(right), max_cells);
        return 0;
    }
    catch (const std::exception& error) { std::cerr << "hot-l2-native: " << error.what() << '\n'; return 1; }
}
