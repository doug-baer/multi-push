# multi-push
pushing files via SFTP using multiple streams per file

This is an experimental project to explore supporting multi-stream "push" of large files from a client to a server by breaking the source file into chunks and then uploading those chunks in parallel into the proper region within a file on the target machine.  Due to support for uploading to a specific ofset within a file, this should be possible. You can use the Paramiko library's _seekable()_ function to see if it is supported

Consider this a proof of concept and it sort of works, but sometimes truncates or otherwise corrupts the file on the remote side. I suspect this has to do with exceeding some threshold regarding th enumbert of concurrent sessions on the server side, though that has yet to be proven. It is not currently tracked, nor is a failed connection retried. 

Example command line
```
./multi-push-sftp.py  --remote_host lvn-catalog --directory_path /hol/lib/cert-export01  --username catalog-mgr --remote_directory /hol/lib/push_test
```

It uses key-based SSH, which must be setup in advance. 

NOTE: By default, OpenSSH on Ubuntu can handle a significant number of simultaneous connections, but reaching, say, 100 concurrent connections will likely require adjustments to the configuration file (/etc/ssh/sshd_config) to ensure optimal performance and avoid potential issues with resource limitations on the server -- like partial transfers due to exhaustion of available SSH connections. 

Configuration Parameter: The primary setting to control the maximum connections is called "MaxStartups" within the sshd_config file.
Default Value: The default value for "MaxStartups" is usually set to a lower number (around 10), which might not be sufficient for handling 100 simultaneous connections.

Recommendation: adjust this value to 100 -- or something that will accommodate the number of connections that you require. The number of connections is the number of simultaneous files multiplied by the number of simultaneous "chunks," with a buffer to account for connections starting before the others are completely closed and cleaned up.

The MaxStartups parameter in OpenSSH specifies the maximum number of concurrent unauthenticated connections to the SSH daemon: 
Default value: 10:30:100

How it works: When the number of unauthenticated connections reaches the MaxStartups value, the server will drop new connection requests at a rate that increases linearly. For example, if the MaxStartups value is 10:30:100, the server will drop connection attempts with a 30% probability if there are 10 unauthenticated connections. 

How to configure: To configure MaxStartups, you can add or adjust the line MaxStartups in the /etc/ssh/sshd_config file. For example, to limit the number of simultaneous unauthenticated connections to 10, you can set the parameter as maxstartups 10:30:60. 



November 18, 2024