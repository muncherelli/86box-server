# Guest networking

Guests never get direct internet access by default. Each **network group** is one private Linux bridge on the host.

## Defining groups

In your inventory (`group_vars/all/main.yml`):

```yaml
netgroups:
  lab: {bridge: br-lab, cidr: 10.86.0.0/24, egress: none, dhcp: false}
  web: {bridge: br-web, cidr: 10.86.1.0/24, egress: none, dhcp: false}
```

- `bridge`: `br-` plus up to 12 of `a-z0-9`.
- `cidr`: IPv4, prefix `/16` to `/29`. Groups must not overlap.
- `egress`: `none` (default) or `nat`. With `nat`, guests reach the internet through the host (masquerade). Optional `egress_allow: [1.1.1.1, 8.8.8.0/24]` limits a nat group to those destinations.
- `dhcp`: see the DHCP section; the default is no DHCP.

`./update.sh` checks these before it changes anything.

## What the host does

- Creates each bridge and gives it the **first host address** of the CIDR (`10.86.0.1` for the example). The host's own network card is never added to a bridge.
- Tells NetworkManager to ignore the bridges and the `tap*` devices 86Box creates.
- Loads its own firewall table, `inet b86box`. It matches only the named bridges, never tap names (86Box picks those), so it cannot touch SSH or other traffic. It does not use Arch's `nftables.service`, which would delete ufw's rules; `./update.sh` refuses to run while that service is enabled.

Result: guests in the same group talk to each other. They cannot reach the host (except DHCP, when enabled), other groups, or the internet.

## Group-to-group routes

Groups cannot see each other unless you list a pair. Pairs work in both directions:

```yaml
group_routes:
  - [lab, web]
```

Both groups must exist in `netgroups`. Guests in a routed pair need the host as gateway (see below), and their traffic is still blocked from reaching the host itself.

## Internet access and ufw

`egress: nat` and `group_routes` turn on IPv4 forwarding (`/etc/sysctl.d/86box.conf`). When ufw is active, `./update.sh` also adds the matching `ufw route allow` rules, because ufw's default forward policy is DROP. Removing a group or a pair later does not delete its ufw rule; remove it by hand (`sudo ufw status numbered`, then `sudo ufw delete <n>`).

## Static addresses inside a guest

- Address: any free address in the group's CIDR except the `.1` host address.
- Gateway: `.1` (only needed for groups with internet access or group-to-group routes).
- Netmask: from the CIDR (`255.255.255.0` for a `/24`).

## Not supported

`br_netfilter` (usually loaded by Docker) changes how bridged traffic is filtered. `./update.sh` stops if it is loaded.
