// CPU-only probes against the selected slop.cpp sources. No model or HTTP.
#include "chat-auto-parser.h"
#include "chat-peg-parser.h"
#include "json-schema-to-grammar.h"
#include <nlohmann/json.hpp>
#include <chrono>
#include <iostream>
using json = nlohmann::ordered_json;

int main() {
    auto started = std::chrono::steady_clock::now();
    json rows = json::array();
    for (const auto & kind : {"string", "nullable", "anyof"}) {
        json schema = {{"type", "string"}};
        if (std::string(kind) == "nullable") schema = {{"type", {"string", "null"}}};
        if (std::string(kind) == "anyof") schema = {{"anyOf", json::array({{{"type", "string"}}, {{"type", "null"}}})}};
        autoparser::generation_params inputs;
        inputs.tools = json::array({{{"type", "function"}, {"function", {{"name", "probe"}, {"parameters", {
            {"type", "object"}, {"properties", {{"value", schema}}}, {"required", {"value"}}
        }}}}}});
        inputs.tool_choice = COMMON_CHAT_TOOL_CHOICE_REQUIRED;
        inputs.parallel_tool_calls = false;
        autoparser::analyze_tools analysis;
        analysis.format.mode = autoparser::tool_format::TAG_WITH_TAGGED;
        analysis.format.per_call_start = "<tool_call>";
        analysis.format.per_call_end = "</tool_call>";
        analysis.function.name_prefix = "<function=";
        analysis.function.name_suffix = ">";
        analysis.function.close = "</function>";
        analysis.arguments.name_prefix = "<parameter=";
        analysis.arguments.name_suffix = ">";
        analysis.arguments.value_suffix = "</parameter>";
        auto parser = build_chat_peg_parser([&](common_chat_peg_builder & p) {
            autoparser::parser_build_context context(p, inputs);
            return analysis.build_parser(context);
        });
        for (const auto & value : json::array({nullptr, "null", "", std::string(64, 'a'), "{\"x\":1}", "olá\nline 2"})) {
            if (std::string(kind) == "string" && value.is_null()) continue;
            // The live template renders string values raw and other values as JSON.
            std::string wire_value = value.is_string() ? value.get<std::string>() : value.dump();
            std::string wire = "<tool_call><function=probe><parameter=value>" + wire_value + "</parameter></function></tool_call>";
            common_peg_parse_context ctx(wire);
            auto parsed = parser.parse(ctx);
            common_chat_msg msg;
            common_chat_peg_mapper(msg).from_ast(ctx.ast, parsed);
            json actual;
            bool valid = parsed.success() && msg.tool_calls.size() == 1;
            if (valid) {
                actual = json::parse(msg.tool_calls[0].arguments, nullptr, false);
                valid = !actual.is_discarded() && actual.contains("value");
                if (valid) actual = actual["value"];
            }
            bool same = valid && actual == value;
            // Exercise every UTF-8 byte prefix through the real lenient parser;
            // incomplete output is observation only and never an executable call.
            size_t prefixes = 0;
            for (size_t n = 0; n <= wire.size(); ++n) {
                std::string part = wire.substr(0, n);
                common_peg_parse_context partial(part, COMMON_PEG_PARSE_FLAG_LENIENT);
                auto result = parser.parse(partial);
                common_chat_msg progress;
                common_chat_peg_mapper(progress).from_ast(partial.ast, result);
                ++prefixes;
                if (n == wire.size() && (progress.tool_calls.size() != msg.tool_calls.size() ||
                    (!msg.tool_calls.empty() && progress.tool_calls[0].arguments != msg.tool_calls[0].arguments))) {
                    throw std::runtime_error("final partial parse differs from complete parse");
                }
            }
            rows.push_back({{"schema", kind}, {"expected", value}, {"actual", actual}, {"preserved", same}, {"prefixes_exercised", prefixes}});
        }
    }
    size_t failures = 0;
    for (const auto & row : rows) if (!row["preserved"].get<bool>()) ++failures;
    auto elapsed = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();
    std::cout << json({{"cases", rows}, {"failures", failures}, {"wall_ms", elapsed}, {"inference_calls", 0},
        {"scope", "Real tagged parser and mapper with fixed Qwen markers; no template analysis, sampler, transport or host lifecycle"}}).dump(2) << '\n';
    return failures ? 1 : 0;
}
