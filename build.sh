#!/bin/bash
# build.sh - compile and link the Virtual Memory Visualizer (bin/vmv).
# Usage: ./build.sh [all|clean]
set -euo pipefail   # [Lab 3] stop on any error, unset variable or failed pipe stage

# [Lab 4] flags kept in an array so each flag stays one word (shellcheck-clean quoting)
CFLAGS=(-Wall -Wextra -g -pthread -fopenmp)

# [Lab 4] source file names without .c; later Parts append names to this array
VMV_SRCS=(main procfs classify render ioutil live workload)

case "${1:-all}" in   # [Lab 4] case on the first argument, "all" if none given
    all)
        mkdir -p build bin
        objs=()
        for name in "${VMV_SRCS[@]}"; do
            echo "gcc ${CFLAGS[*]} -c src/$name.c -o build/$name.o"
            # [Lab 2] compile one .c file into an object file (.o), no linking yet
            gcc "${CFLAGS[@]}" -c "src/$name.c" -o "build/$name.o"
            objs+=("build/$name.o")
        done
        echo "gcc ${CFLAGS[*]} -o bin/vmv ${objs[*]}"
        # [Lab 2] link all object files into one executable
        gcc "${CFLAGS[@]}" -o bin/vmv "${objs[@]}"
        echo "gcc ${CFLAGS[*]} -c src/demo_main.c -o build/demo_main.o"
        # [Lab 2] compile the standalone demo program
        gcc "${CFLAGS[@]}" -c src/demo_main.c -o build/demo_main.o
        echo "gcc ${CFLAGS[*]} -o bin/vmv_demo build/demo_main.o build/workload.o"
        # [Lab 2] link it with the shared workload.o (same object used by bin/vmv)
        gcc "${CFLAGS[@]}" -o bin/vmv_demo build/demo_main.o build/workload.o
        chmod 755 bin/*   # [Lab 2] rwxr-xr-x: owner may write, everyone may run
        echo "Build OK: bin/vmv"
        ;;
    clean)
        rm -rf build bin
        ;;
    *)
        echo "Usage: ./build.sh [all|clean]"
        exit 1
        ;;
esac
