using System.Net;
using System.Net.Http;
using System.Net.NetworkInformation;
using System.Net.Sockets;

namespace CampusConnect;

internal static class Connectivity
{
    // A plain system ping could use the OTHER interface when Ethernet and Wi-Fi
    // are both connected. Bind each probe to the target interface IPv4 address.
    internal static IPAddress? FindIpv4(Guid? interfaceId)
    {
        if (interfaceId is null) return null;
        try {
            var active = NetworkInterface.GetAllNetworkInterfaces().FirstOrDefault(n =>
                Guid.TryParse(n.Id, out var id) && id == interfaceId && n.OperationalStatus == OperationalStatus.Up);
            return active?.GetIPProperties().UnicastAddresses
                .Select(address => address.Address)
                .FirstOrDefault(ip => ip.AddressFamily == AddressFamily.InterNetwork &&
                    !IPAddress.IsLoopback(ip) && !ip.ToString().StartsWith("169.254.", StringComparison.Ordinal));
        } catch (NetworkInformationException) { return null; }
    }
    internal static bool HasUsableIp(Guid? interfaceId) => FindIpv4(interfaceId) is not null;
    public static async Task<InternetStatus> CheckAsync(Guid? interfaceId)
    {
        var localIp = FindIpv4(interfaceId);
        if (localIp is null) return InternetStatus.Unavailable;
        try {
            using var handler = new SocketsHttpHandler {
                UseProxy = false,
                AllowAutoRedirect = false,
                ConnectTimeout = TimeSpan.FromSeconds(4),
                ConnectCallback = async (context, cancellation) => {
                    var addresses = await Dns.GetHostAddressesAsync(context.DnsEndPoint.Host, cancellation);
                    var ipv4 = addresses.Where(ip => ip.AddressFamily == AddressFamily.InterNetwork).ToArray();
                    if (ipv4.Length == 0) throw new SocketException((int)SocketError.HostNotFound);
                    Exception? last = null;
                    foreach (var remote in ipv4) {
                        var socket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
                        try {
                            socket.Bind(new IPEndPoint(localIp, 0));
                            await socket.ConnectAsync(new IPEndPoint(remote, context.DnsEndPoint.Port), cancellation);
                            return new NetworkStream(socket, ownsSocket: true);
                        } catch (Exception ex) when (ex is SocketException or OperationCanceledException) {
                            socket.Dispose(); last = ex;
                            if (cancellation.IsCancellationRequested) throw;
                        }
                    }
                    throw last ?? new SocketException((int)SocketError.NetworkUnreachable);
                }
            };
            using var client = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(5) };
            using var expected = await client.GetAsync("http://www.msftconnecttest.com/connecttest.txt",
                HttpCompletionOption.ResponseContentRead);
            if (expected.IsSuccessStatusCode &&
                (await expected.Content.ReadAsStringAsync()).Trim() == "Microsoft Connect Test")
                return InternetStatus.Online;
            // The same adapter may be usable even if the Windows HTTP probe is filtered.
        } catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or SocketException
            or TimeoutException or System.IO.IOException or InvalidOperationException) {
            // Try a separate HTTPS origin with the same interface-bound socket.
        }
        try {
            using var handler = new SocketsHttpHandler {
                UseProxy = false, AllowAutoRedirect = false, ConnectTimeout = TimeSpan.FromSeconds(4),
                ConnectCallback = async (context, cancellation) => {
                    var addresses = await Dns.GetHostAddressesAsync(context.DnsEndPoint.Host, cancellation);
                    var remote = addresses.FirstOrDefault(ip => ip.AddressFamily == AddressFamily.InterNetwork)
                        ?? throw new SocketException((int)SocketError.HostNotFound);
                    var socket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
                    try {
                        socket.Bind(new IPEndPoint(localIp, 0));
                        await socket.ConnectAsync(new IPEndPoint(remote, context.DnsEndPoint.Port), cancellation);
                        return new NetworkStream(socket, ownsSocket: true);
                    } catch { socket.Dispose(); throw; }
                }
            };
            using var client = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(5) };
            using var result = await client.GetAsync("https://www.baidu.com/", HttpCompletionOption.ResponseHeadersRead);
            return result.IsSuccessStatusCode ? InternetStatus.Online : InternetStatus.Unavailable;
        } catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or SocketException
            or TimeoutException or System.IO.IOException or InvalidOperationException) {
            return InternetStatus.Unavailable;
        }
    }

    // Retained for platform-neutral unit testing of the ping helper; the app's
    // real connectivity result uses interface-bound HTTP probes above.
    internal static async Task<InternetStatus> CheckWithPingAsync(Func<Task<IPStatus>> send, TimeSpan? timeout = null)
    {
        try {
            var status = await send().WaitAsync(timeout ?? TimeSpan.FromSeconds(4));
            return status == IPStatus.Success ? InternetStatus.Online : InternetStatus.Unavailable;
        } catch (Exception ex) when (ex is PingException or TimeoutException or OperationCanceledException or
                                    System.Net.Sockets.SocketException or InvalidOperationException) {
            return InternetStatus.Unavailable;
        }
    }
}

internal sealed record NetworkLink(Guid Id, string Name, NetworkInterfaceType Type, OperationalStatus Status);

// Ethernet link detection must work before DHCP / PPPoE / 802.1X has supplied an IP address.
internal static class CampusNetwork
{
    internal static bool IsWiredInterface(NetworkInterfaceType type, OperationalStatus status)
        => status == OperationalStatus.Up && type is NetworkInterfaceType.Ethernet
            or NetworkInterfaceType.GigabitEthernet or NetworkInterfaceType.FastEthernetFx
            or NetworkInterfaceType.FastEthernetT;
    public static IReadOnlyList<NetworkLink> ReadLinks()
    {
        try {
            return NetworkInterface.GetAllNetworkInterfaces().Where(n => Guid.TryParse(n.Id, out _))
                .Select(n => new NetworkLink(Guid.Parse(n.Id), n.Name, n.NetworkInterfaceType, n.OperationalStatus)).ToArray();
        } catch (NetworkInformationException) { return []; }
    }
    internal static NetworkLink? SelectEthernet(string preferred, IEnumerable<NetworkLink> links)
        => links.FirstOrDefault(n => IsWiredInterface(n.Type, n.Status)
            && (string.IsNullOrEmpty(preferred) || n.Name.Equals(preferred, StringComparison.OrdinalIgnoreCase)));
    internal static NetworkLink? SelectPpp(string name, IEnumerable<NetworkLink> links)
        => links.FirstOrDefault(n => n.Type == NetworkInterfaceType.Ppp && n.Status == OperationalStatus.Up
            && !string.IsNullOrWhiteSpace(name) && n.Name.Equals(name, StringComparison.OrdinalIgnoreCase));
    public static Guid? ReadWiredInterface() => SelectEthernet("", ReadLinks())?.Id;
    public static NetworkLink? ReadPhysicalLink(CampusProfile profile) => SelectEthernet(profile.WiredAuth == "pppoe" ? "" : profile.EthernetName, ReadLinks());
    public static Guid? ReadWiredInterface(CampusProfile profile)
    {
        var links = ReadLinks();
        var ethernet = SelectEthernet(profile.WiredAuth == "pppoe" ? "" : profile.EthernetName, links);
        return ethernet is null ? null : profile.WiredAuth == "pppoe" ? SelectPpp(profile.BroadbandName, links)?.Id : ethernet.Id;
    }
    public static Task<bool> IsAvailableAsync(CampusProfile profile)
        => IsAvailableAsync(profile, () => ReadWiredInterface(profile), WifiService.ReadCurrentAsync);
    internal static async Task<bool> IsAvailableAsync(CampusProfile profile,
        Func<Guid?> readWired, Func<string?, Task<string?>> readWifi)
        => profile.IsWired ? readWired() is not null
            : profile.MatchesNetwork(await readWifi(profile.Ssid), false);
    public static Task<Guid?> ReadInterfaceAsync(CampusProfile profile)
        => profile.IsWired ? Task.FromResult(ReadWiredInterface(profile)) : WifiService.ReadInterfaceAsync(profile.Ssid);
}
