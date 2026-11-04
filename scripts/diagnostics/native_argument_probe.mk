# Run from any cwd; source and build roots must be explicit, without spaces.
ifndef SLOP_ROOT
$(error SLOP_ROOT must select the slop.cpp checkout to test)
endif
ifndef BUILD
$(error BUILD must select an owned disposable build directory)
endif
HERE := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
CXXFLAGS := -std=c++17 -O0 -ffunction-sections -fdata-sections -MMD -MP
CPPFLAGS := -I$(SLOP_ROOT)/common -I$(SLOP_ROOT)/include -I$(SLOP_ROOT)/ggml/include -I$(SLOP_ROOT)/vendor
SOURCES := chat-auto-parser-generator chat-peg-parser peg-parser json-schema-to-grammar unicode log common console chat-diff-analyzer chat-auto-parser-helpers trie
OBJECTS := $(addprefix $(BUILD)/,$(addsuffix .o,$(SOURCES))) $(BUILD)/probe.o
.PHONY: all
all: $(BUILD)/native-argument-probe
$(BUILD)/native-argument-probe: $(OBJECTS)
	$(CXX) $(OBJECTS) -Wl,--gc-sections -pthread -o $@
$(BUILD)/probe.o: $(HERE)/native_argument_probe.cpp
	@mkdir -p $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@
$(BUILD)/%.o: $(SLOP_ROOT)/common/%.cpp
	@mkdir -p $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@
-include $(OBJECTS:.o=.d)
