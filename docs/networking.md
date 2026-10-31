# Guest networking

Guests never get direct internet access by default. Each **network group** is one private Linux bridge on the host.

## What a group means for a VM

A group is a private virtual switch. On the VM's edit page you choose which groups the VM is **allowed** to use. Then, in 86Box's Settings window, you add a network card and set its network type:

- **None** (the default): the card exists in the guest but is connected to nothing. This is always allowed and needs no group.
- **TAP**: connects the card to a host bridge. The bridge must belong to one of the VM's allowed groups. If it doesn't, the config guard stops the VM and the VM list says why.
- Any other type (SLiRP, PCap, VDE) is refused, because it would bypass the groups.

So a VM with no groups still works. It just has no network. Guests in the same group can talk to each other; reaching the host, other groups or the internet is up to the group's settings below.

## Defining and changing groups

> **After the first `./update.sh`, groups are managed in the web manager** (the Network groups page), not in the
> inventory. The `netgroups`, `group_routes` and `nat_dns` settings in `group_vars/all/main.yml` are only the
> starting point: `./update.sh` reads them once, on a host that has no saved network settings yet, and ignores
> them after that. Changing them later does nothing.

The first-time settings look like this:

```yaml
netgroups:
  lab: {cidr: 10.86.0.0/24, egress: none, dhcp: false}
  web: {cidr: 10.86.1.0/24, egress: none, dhcp: false}
```

- Group name: 1-12 characters, `a-z` and `0-9`, starting with a letter.
- `bridge`: optional; the default is `br-` plus the group name. `br-` plus up to 12 of `a-z0-9`.
- `cidr`: a private IPv4 network (`10.x`, `172.16-31.x` or `192.168.x`), prefix `/16` to `/29`. Groups must not overlap each other, and a group may not use a network that is already in use on the host (its own network card, Docker and so on).
- `egress`: `none` (default) or `nat`. With `nat`, guests reach the internet through the host (masquerade). Optional `egress_allow: [1.1.1.1, 8.8.8.0/24]` limits a nat group to those destinations.
- `dhcp`: see the DHCP section; the default is no DHCP.
- At most 32 groups.

### How a change is applied

The web manager never changes the network itself. It writes a request file, and a root helper
(`/opt/86box-server/libexec/netgroups-apply`, started by `86box-netapply.path`) checks every part of it and then
makes the host match. A request that fails any check is refused as a whole and nothing changes. The answer, with
any warnings, is in `/run/86box-server/netapply.json`. The saved groups are in `/etc/86box-server/netgroups.json`
and `netpolicy.json`.

- **Deleting a group** removes its firewall rules, DHCP range and bridge. If a VM is still plugged into the bridge, the bridge is kept without an address and a warning says so; stop the VM and apply again.
- **Changing a network** gives the bridge the new `.1` address; guests need new addresses.
- The host's own network card, its addresses and its routes are never touched.

## What the host does

- Creates each bridge and gives it the **first host address** of the CIDR (`10.86.0.1` for the example). The host's own network card is never added to a bridge.
- Tells NetworkManager to ignore the bridges and the `tap*` devices 86Box creates.
- Loads its own firewall table, `inet b86box`. It matches only the named bridges, never tap names (86Box picks those), so it cannot touch SSH or other traffic. It does not use Arch's `nftables.service`, which would delete ufw's rules; `./update.sh` refuses to run while that service is enabled.

Result: guests in the same group talk to each other. They cannot reach the host (except DHCP, when enabled), other groups, or the internet.

## Group-to-group routes

Groups cannot see each other unless you list a pair. Pairs work in both directions (first-time setting: `group_routes: [[lab, web]]`).

Both groups must exist. Guests in a routed pair need the host as gateway (see below), and their traffic is still blocked from reaching the host itself.

## Internet access and ufw

`egress: nat` and `group_routes` turn on IPv4 forwarding (`/etc/sysctl.d/86box.conf`). When ufw is active, `./update.sh` also adds the matching `ufw route allow` rules, because ufw's default forward policy is DROP. Removing a group or a pair removes its ufw rules again.

## DHCP (optional)

Set `dhcp: true` on a group and `./update.sh` runs one small `dnsmasq` for all such groups (`86box-dnsmasq.service`). It does DHCP only, no DNS server. Static addresses always work too, and NT4 is fine with them.

- **Range:** `.100` to `.199` of the group's network. Groups too small for that (`/25` and smaller) use the upper half of the network instead.
- **Lease time:** 12 hours. Leases are listed in `<vm_data_root>/dnsmasq.leases`.
- **Gateway handed out:** the host address (`.1`) only when the group has internet access (`egress: nat`) or is part of a group pair in `group_routes`. Otherwise guests get no gateway.
- **DNS handed out:** `nat_dns` (default `1.1.1.1`) for `nat` groups, none for the rest.
- **Firewall:** port 67/udp is opened on that group's bridge only (in the host's own table, and in ufw when it is active). Turning `dhcp` off closes it again and stops and removes the service.

Arch's own `dnsmasq.service` must stay off; `./update.sh` refuses to run while it is active.

## Static addresses inside a guest

- Address: any free address in the group's CIDR except the `.1` host address.
- Gateway: `.1` (only needed for groups with internet access or group-to-group routes).
- Netmask: from the CIDR (`255.255.255.0` for a `/24`).

## Not supported

`br_netfilter` (usually loaded by Docker) changes how bridged traffic is filtered. `./update.sh` stops if it is loaded.
