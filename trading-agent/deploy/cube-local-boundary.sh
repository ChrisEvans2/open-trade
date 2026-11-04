#!/usr/bin/env bash
# Single-node Cube boundary. Own chains only; never restore/flush host rules.
set -euo pipefail
action="${1:?usage: cube-local-boundary.sh apply|remove PUBLIC_INTERFACE}"
public_interface="${2:?explicit public interface required}"
[[ "$public_interface" =~ ^[a-zA-Z0-9_.:-]+$ ]] || exit 2
[[ "$action" == apply || "$action" == remove ]] || exit 2
for firewall in iptables ip6tables; do
  for spec in 'INPUT HERMES-CUBE-IN' 'FORWARD HERMES-CUBE-FWD'; do
    read -r parent chain <<< "$spec"
    if [[ "$action" == remove ]]; then
      if [[ "$parent" == FORWARD ]]; then
        while "$firewall" -w -C DOCKER-USER -j "$chain" 2>/dev/null; do
          "$firewall" -w -D DOCKER-USER -j "$chain"
        done
      fi
      while "$firewall" -w -C "$parent" -j "$chain" 2>/dev/null; do
        "$firewall" -w -D "$parent" -j "$chain"
      done
      if "$firewall" -w -S "$chain" >/dev/null 2>&1; then
        "$firewall" -w -F "$chain"
        "$firewall" -w -X "$chain"
      fi
      continue
    fi
    "$firewall" -w -S "$chain" >/dev/null 2>&1 || "$firewall" -w -N "$chain"
    if [[ "$parent" == INPUT ]]; then
      for ports in 80,14443,9090,8082,3000,3010,8089,8090,8083 9998,9999,9966,9091,9000,9001,12088; do
        rule=(-i "$public_interface" -p tcp -m multiport --dports "$ports" -j REJECT)
        "$firewall" -w -C "$chain" "${rule[@]}" 2>/dev/null || "$firewall" -w -A "$chain" "${rule[@]}"
      done
    else
      # Docker publishes bypass INPUT; original destination survives DNAT.
      for port in 9000 9001 12088; do
        rule=(-i "$public_interface" -p tcp -m conntrack --ctorigdstport "$port" -j REJECT)
        "$firewall" -w -C "$chain" "${rule[@]}" 2>/dev/null || "$firewall" -w -A "$chain" "${rule[@]}"
      done
    fi
    "$firewall" -w -C "$parent" -j "$chain" 2>/dev/null || "$firewall" -w -I "$parent" 1 -j "$chain"
    if [[ "$parent" == FORWARD ]] && "$firewall" -w -S DOCKER-USER >/dev/null 2>&1; then
      # Docker may prepend its own FORWARD rules on restart; its user chain is stable.
      "$firewall" -w -C DOCKER-USER -j "$chain" 2>/dev/null || "$firewall" -w -I DOCKER-USER 1 -j "$chain"
    fi
  done
done
